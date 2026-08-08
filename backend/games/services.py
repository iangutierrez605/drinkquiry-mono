"""Game construction + live-mutation logic, kept out of views/consumers for
testability. The WebSocket consumer wraps the mutation functions below with
`database_sync_to_async`; `games/tests.py` calls them directly.

Handoff #8: the buzzer/judging/drink mutations moved here too (they were the
consumer's last inline bodies) because §F changes judging (reveal-on-correct
plus the judgment marker) and §G changes drink assignment (once-per-cell,
player-initiated) — per the house rule, changed mutations live in
services.py where the suite exercises exactly what the socket runs."""
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Abs
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from trivia.models import Category, ModerationStatus, Question, Visibility

from .models import (
    BoardCell,
    BoardColumn,
    Buzz,
    CellState,
    DrinkAssignment,
    Game,
    GameMode,
    GameStatus,
    Participant,
    ParticipantRole,
)


class ActionError(Exception):
    """A host/player action that can't be applied; the consumer relays the
    message to the sender as an {type: "error"} event."""


class StructuredActionError(ActionError):
    """An ActionError carrying a documented structured payload (lesson C4:
    exact shape, no extra keys). The consumer spreads `payload` into the
    error frame: {"type": "error", "detail": ..., "code": ...}."""

    def __init__(self, detail: str, code: str):
        super().__init__(detail)
        self.payload = {"detail": detail, "code": code}


PUBLIC_APPROVED = Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)

# §F2 (Handoff #21), C-3: the chug-wager bounds. The host is the referee
# (same trust model as judging) — the server just keeps the number sane.
THUNDER_WAGER_MIN = 3
THUNDER_WAGER_MAX = 30


def _pick_thunder_cells(game: Game) -> list[BoardCell]:
    """§F1b (Handoff #21), C-1: mark this board's THUNDER FUCKED cells.

    Drinks boards ONLY (points boards never get one — pinned). Count by
    TOTAL cells: 2 on boards with >= 10, 1 on 2–9, 0 on a 1-cell board.
    Placement is uniformly random EXCLUDING row 0 (the cheap top row) with
    at most one per column — chosen server-side at create so the board is
    fixed before anyone joins. Kept as ONE small function so the suite can
    call it directly against a built board (C-1's "deterministic access"
    is the DB + the host-private board view; there is deliberately NO
    test-only create param).

    Edge (recorded ruling): a 1-row board (questions_per_category=1) has
    only row-0 cells, so even a 2–9-cell board of that shape marks NOTHING
    — the row-0 exclusion outranks the count. Same if eligible columns run
    out (can't happen with row-0 excluded and count <= 2 unless the board
    has < count columns holding a row > 0 cell).
    """
    import random

    if game.mode != GameMode.DRINKS:
        return []
    cells = list(game.cells.all())
    total = len(cells)
    count = 0 if total < 2 else (2 if total >= 10 else 1)
    if count == 0:
        return []
    by_column: dict[int, list[BoardCell]] = {}
    for cell in cells:
        if cell.row == 0:
            continue  # C-1: never the cheap top row
        by_column.setdefault(cell.column_id, []).append(cell)
    column_ids = list(by_column)
    random.shuffle(column_ids)
    chosen: list[BoardCell] = []
    for column_id in column_ids[:count]:  # at most one per column
        chosen.append(random.choice(by_column[column_id]))
    for cell in chosen:
        cell.is_thunder = True
        cell.save(update_fields=["is_thunder"])
    return chosen


def usable_questions(category: Category, user):
    """Questions this user is allowed to put on a board from this category.
    §I (Handoff #9): soft-deleted questions never enter new boards (nor the
    §J3 replace pool) — cells already holding one keep it (history).
    §F2 (#18): archived questions (the venue shelf) never enter new boards
    either, same cells-keep-history rule."""
    allowed = PUBLIC_APPROVED | Q(owner__isnull=True)
    if user and user.is_authenticated:
        allowed = allowed | Q(owner=user)
    return category.questions.filter(allowed, deleted_at__isnull=True, is_archived=False)


def _preference_ordered(qs, host):
    """§J2 draw preference, applied WITHIN each difficulty tie elsewhere:
    annotate usage so ordering can put (a) questions never used in any of
    THIS HOST's games first (host_uses=0 sorts before everything), then
    (b) least-used-by-this-host, tiebreak least-used-globally, then random.
    Usage is derived from the cells table (§J1: derive-first, no counters);
    distinct=True keeps the two aggregates over the same join honest."""
    return qs.annotate(
        host_uses=Count("boardcell", filter=Q(boardcell__game__host=host), distinct=True),
        global_uses=Count("boardcell", distinct=True),
    )


def _cell_value(mode: str, row: int) -> int:
    """Row → value scaling, the ONE place it lives (Handoff #16 §F): drinks
    count 1..n down the column, points 100..n*100. create_game and the
    column swap both call this so a swapped column can never scale
    differently from a created one."""
    return (row + 1) if mode == GameMode.DRINKS else (row + 1) * 100


def _draw_for_category(*, category, host, questions_per_category: int, exclude_ids):
    """The ONE per-column draw body (Handoff #16 §F: factored out of
    create_game so the column swap can't drift from creation behavior).

    §J2: difficulty is the primary key; within a difficulty the host's
    unused questions come first, then least-used, then random — over a 3x
    fetch window, with a stable easiest→hardest re-sort so value scales
    with row. `exclude_ids` is the never-twice-on-one-board set (§F3).

    Returns (chosen, pool_size): `chosen` is the ordered pick, or None when
    the post-exclusion pool can't fill the column (`pool_size` then feeds
    the caller's shortage message — create_game's format, shared)."""
    qs = list(
        _preference_ordered(usable_questions(category, host), host)
        .exclude(id__in=exclude_ids)  # §F3: never twice on one board
        .order_by("difficulty", "host_uses", "global_uses", "?")[: questions_per_category * 3]
    )
    if len(qs) < questions_per_category:
        return None, len(qs)
    # Sort chosen subset easiest->hardest so value scales with row.
    # Python's sort is stable, so the §J2 preference order within each
    # difficulty survives the re-sort.
    chosen = sorted(qs, key=lambda q: q.difficulty)[:questions_per_category]
    return chosen, len(qs)


@transaction.atomic
def create_game(
    *,
    host,
    mode: str,
    category_ids: list[int],
    questions_per_category: int,
    buzz_sound: int = 1,
    thunder: bool = True,
    tournament=None,
    round_number: int | None = None,
    hand_picked: dict | None = None,
) -> Game:
    """Build a game board from `category_ids`, in the order given.

    §H (Handoff #13): `buzz_sound` is the host's per-game sound choice (1–4,
    the four synthesized voices) — set at creation ONLY (mid-game change is
    §M). Validated here as well as in the serializer because the suite calls
    this directly (rule 4 lives where the mutation lives).

    §I (Handoff #13): `tournament` is an already-RESOLVED Tournament instance
    (or None) — ownership, liveness and the finished check are the VIEW's job
    (they're HTTP-shaped: owner-scoped 404, the pinned tournament_finished
    409 — the quota_denial placement pattern). Here we only enforce the
    pairing: a tournament game always knows its round, a plain game has
    neither.

    §F5 (Handoff #18): `hand_picked` maps category id → an ORDERED list of
    question ids for that column; any subset of the picked categories may
    appear, and absent columns keep the automatic draw. The PAID gate is the
    view's job (HTTP-shaped 403); everything content-shaped is enforced
    here: each list's length must equal `questions_per_category`, every id
    must be usable by this host from THAT category (own / official /
    approved-public per C-3's default — the same usable_questions filter
    the draw uses, so deleted and archived rows are out), and no id may
    repeat anywhere on the board. Violations 400 with the offenders listed
    (the shortage-409 honesty, in ValidationError form).

    ORDERING RULING (§F5, deliberate divergence from _draw_for_category):
    a hand-picked column's row order IS the given list order — the host's
    climb — SKIPPING the automatic easiest→hardest re-sort. Value scaling
    by row is unchanged (_cell_value), so the host's first pick is the
    1-drink/100-point row regardless of its difficulty label.

    §G (Handoff #10), deliberate: this API is THEME-UNAWARE. Themes are a
    discovery/selection layer on the host create screen — they filter and
    pre-select categories client-side, and the request still arrives as
    `category_ids`. Do not "improve" this into a theme_id parameter without
    an actual reason; the server-side theme-aware build is punted (§M).

    §F3: with categories M2M, overlapping columns share questions — the
    per-column draw excludes everything already picked (see below), so a
    question appears at most once per board and shortage is judged against
    the post-exclusion pool.
    """
    if not (1 <= questions_per_category <= 10):
        raise ValidationError({"questions_per_category": "Must be between 1 and 10."})
    if buzz_sound not in (1, 2, 3, 4):
        raise ValidationError({"buzz_sound": "Pick one of the four sounds (1–4)."})
    if (tournament is None) != (round_number is None):
        raise ValidationError({"round_number": "A tournament game needs both a tournament and a round number."})
    if round_number is not None and round_number < 1:
        raise ValidationError({"round_number": "Rounds start at 1."})
    if not (1 <= len(category_ids) <= 8):
        raise ValidationError({"categories": "Pick between 1 and 8 categories."})
    if len(set(category_ids)) != len(category_ids):
        raise ValidationError({"categories": "Duplicate categories are not allowed."})

    # §F5: normalize + type-check hand_picked here too (rule 4 — the suite
    # calls this directly; JSON keys arrive as strings from the serializer).
    picks_by_category: dict[int, list[int]] = {}
    if hand_picked:
        if not isinstance(hand_picked, dict):
            raise ValidationError({"hand_picked": "Expected an object of category id → question ids."})
        for raw_key, raw_list in hand_picked.items():
            try:
                key = int(raw_key)
            except (TypeError, ValueError):
                raise ValidationError({"hand_picked": f"'{raw_key}' isn't a category id."})
            if not isinstance(raw_list, (list, tuple)) or not all(
                isinstance(v, int) and not isinstance(v, bool) for v in raw_list
            ):
                raise ValidationError({"hand_picked": f"Category {key}: expected a list of question ids."})
            if key not in category_ids:
                raise ValidationError(
                    {"hand_picked": f"Category {key} isn't one of the picked categories."}
                )
            picks_by_category[key] = list(raw_list)

    # §F5: a deleted category id reads as "Unknown category ids" — deleted
    # categories are invisible to new game builds, full stop.
    categories = list(Category.objects.filter(id__in=category_ids, deleted_at__isnull=True))
    found = {c.id for c in categories}
    if missing := set(category_ids) - found:
        raise ValidationError({"categories": f"Unknown category ids: {sorted(missing)}."})

    # Requirement 4: refuse to create the game if any chosen category is short
    # on questions the host may use.
    #
    # §F3 (Handoff #10): categories are M2M now, so a question can sit in TWO
    # selected categories — it must appear AT MOST ONCE on the board. Columns
    # are processed IN THE ORDER GIVEN, each excluding every question already
    # picked by an earlier column (`used_question_ids` — the same idea
    # replace_cell_question uses with `on_board`). The shortage check runs
    # against the EXCLUDED pool, so "Movies (5) + 80s Movies (5) sharing 3"
    # correctly refuses a 5-per-category build instead of silently
    # duplicating. Shortage ATTRIBUTION is therefore order-dependent — the
    # LATER of two overlapping columns reports short (acceptable; the error
    # shape and message format are unchanged).
    by_id = {c.id: c for c in categories}
    shortages = {}
    hp_errors: list[str] = []
    picked: dict[int, list[Question]] = {}
    used_question_ids: set[int] = set()
    for category_id in category_ids:
        category = by_id[category_id]
        picked_ids = picks_by_category.get(category_id)
        if picked_ids is not None:
            # §F5: hand-picked column — validate, don't draw. All problems
            # for all columns are collected, then 400ed together (the
            # shortage-collection pattern below).
            problems = []
            if len(picked_ids) != questions_per_category:
                problems.append(
                    f"pick exactly {questions_per_category} questions ({len(picked_ids)} given)"
                )
            dups_in_column = {pid for pid in picked_ids if picked_ids.count(pid) > 1}
            if dups_in_column:
                problems.append(f"question ids repeated in the column: {sorted(dups_in_column)}")
            usable = {
                q.id: q for q in usable_questions(category, host).filter(id__in=picked_ids)
            }
            unusable = sorted({pid for pid in picked_ids if pid not in usable})
            if unusable:
                problems.append(
                    f"question ids not usable from this category: {unusable}"
                )
            on_board_already = sorted(
                {pid for pid in picked_ids if pid in used_question_ids}
            )
            if on_board_already:
                problems.append(
                    f"question ids already on the board: {on_board_already}"
                )
            if problems:
                hp_errors.append(f"'{category.name}': " + "; ".join(problems) + ".")
                continue
            chosen = [usable[pid] for pid in picked_ids]  # the host's order, kept
            picked[category.id] = chosen
            used_question_ids.update(picked_ids)
            continue
        # §J2 preference + 3x window + stable easiest->hardest re-sort all
        # live in _draw_for_category now (§F #16: shared with the column
        # swap so the two can't drift). The shortage contract and row/value
        # placement behave exactly as they did.
        chosen, pool_size = _draw_for_category(
            category=category,
            host=host,
            questions_per_category=questions_per_category,
            exclude_ids=used_question_ids,
        )
        if chosen is None:
            shortages[category.name] = pool_size
        else:
            picked[category.id] = chosen
            used_question_ids.update(q.id for q in chosen)
    if hp_errors:
        raise ValidationError({"hand_picked": hp_errors})
    if shortages:
        raise ValidationError(
            {
                "categories": [
                    f"'{name}' only has {count} usable question(s); {questions_per_category} required."
                    for name, count in shortages.items()
                ]
            }
        )

    game = Game.objects.create(
        host=host,
        mode=mode,
        questions_per_category=questions_per_category,
        buzz_sound=buzz_sound,
        tournament=tournament,
        round_number=round_number,
    )

    ordered = {c.id: c for c in categories}
    for position, category_id in enumerate(category_ids):
        column = BoardColumn.objects.create(game=game, category=ordered[category_id], position=position)
        for row, question in enumerate(picked[category_id]):
            BoardCell.objects.create(
                game=game, column=column, question=question, row=row, value=_cell_value(mode, row)
            )
    # §F1 (#21): mark the board's THUNDER FUCKED cells (drinks only, C-1).
    # After the board loop so the pick sees the finished grid; inside this
    # same transaction so no snapshot can ever catch a half-marked board.
    # #21.1: `thunder=False` (the create-screen opt-out) skips the marking
    # entirely — the board is fully ⚡-free forever (nothing stored, nothing
    # to toggle later); the email/PDF naturally carry no markers. Validated
    # strict-bool here because the suite calls this directly (rule 4).
    if not isinstance(thunder, bool):
        raise ValidationError({"thunder": ["Must be true or false."]})
    if thunder:
        _pick_thunder_cells(game)
    if tournament is not None:
        _auto_target_advancers(tournament=tournament, next_round=round_number)
    return game


@transaction.atomic
def replace_cell_question(*, code: str, cell_id, host) -> BoardCell:
    """§J3: lobby-only redraw of ONE cell — same category, closest difficulty
    to the question being replaced (the cell's difficulty slot), §J2's
    preference order, excluding every question already on this board. Raises
    ActionError with a message the view maps to a structured 409; host auth
    is the VIEW's job (this runs under Knox, not a participant token)."""
    game = Game.objects.select_for_update().get(code=code)
    if game.status != GameStatus.LOBBY:
        raise ActionError("The game has started — questions can only be replaced in the lobby.")
    cell = game.cells.filter(pk=cell_id).first()
    if cell is None:
        raise ActionError("No such cell in this game.")
    old_question = cell.question
    on_board = set(game.cells.values_list("question_id", flat=True))
    candidate = (
        _preference_ordered(usable_questions(cell.column.category, host), host)
        .exclude(id__in=on_board)
        .annotate(difficulty_gap=Abs(F("difficulty") - Value(old_question.difficulty)))
        .order_by("difficulty_gap", "host_uses", "global_uses", "?")
        .first()
    )
    if candidate is None:
        raise ActionError("No other usable questions in this category to swap in.")
    cell.question = candidate
    cell.save(update_fields=["question"])
    return cell


@transaction.atomic
def replace_column_category(*, code: str, column_id, new_category_id, host) -> BoardColumn:
    """§F (Handoff #16): lobby-only swap of ONE column's category — the
    board-edit precedent (replace_cell_question, directly above) extended to
    a whole column. Draws `questions_per_category` fresh questions from the
    incoming category via the SAME body creation uses (_draw_for_category:
    §J2 preference, 3x window, easiest→hardest), tears the old cells down
    and rebuilds with the same row/value scaling (_cell_value).

    LOBBY-ONLY, deliberately (the v1 line cell-replace already drew): a
    played column carries answered cells, buzz history and awarded scores —
    swapping it mid-game is a correctness swamp, punted (§M; the safe
    mid-game scope would be "all cells still HIDDEN", a separate piece).

    The never-twice-on-one-board exclusion is judged against the POST-SWAP
    board: everything on the board EXCEPT this column's own cells (they're
    torn down in this same transaction, so their questions return to the
    pool). Deliberately NOT the literal whole-board set — with categories
    M2M, a question shared by the outgoing and incoming categories is a
    legitimate pick for the rebuilt column, and excluding it would
    manufacture spurious shortages for zero invariant gain (pinned by
    test_swap_may_reuse_a_question_shared_with_the_outgoing_column).

    Raises ActionError with messages the view maps to structured 409s; host
    auth is the VIEW's job (this runs under Knox, not a participant token).
    """
    game = Game.objects.select_for_update().get(code=code)
    if game.status != GameStatus.LOBBY:
        raise ActionError("The game has started — categories can only be swapped in the lobby.")
    column = game.columns.filter(pk=column_id).first()
    if column is None:
        raise ActionError("No such column in this game.")
    # §F5 house rule: a soft-deleted category is invisible to board edits,
    # exactly as it is to create_game ("Unknown category ids").
    new_category = Category.objects.filter(pk=new_category_id, deleted_at__isnull=True).first()
    if new_category is None:
        raise ActionError("Unknown category.")
    # §F4/§F7 (#18): the swap-in mirrors creation's hosting gates, in this
    # path's ActionError→409 dialect. A host's own bound category needs its
    # pack ACTIVE; a host's own UNBOUND custom category needs authoring
    # rights (plan/overrides/venue — billing/access.can_host_own_custom).
    # Foreign/public categories pass untouched (library play stays free).
    from billing.access import can_host_own_custom

    if new_category.entitlement_id is not None and new_category.owner_id == host.id:
        if not new_category.entitlement.is_active:
            raise ActionError(
                "That category's pack window has ended — reactivate the pack to host it."
            )
    elif new_category.owner_id == host.id and not can_host_own_custom(host):
        raise ActionError(
            "Hosting your own custom categories needs an active plan or pack."
        )
    # unique(game, category) would reject the duplicate anyway — check first
    # for the friendly message. The column's OWN category counts too: a
    # same-category "swap" is just this 409, not a hidden full-redraw verb.
    if game.columns.filter(category=new_category).exists():
        raise ActionError("That category is already on the board.")
    keep_ids = set(game.cells.exclude(column=column).values_list("question_id", flat=True))
    chosen, pool_size = _draw_for_category(
        category=new_category,
        host=host,
        questions_per_category=game.questions_per_category,
        exclude_ids=keep_ids,
    )
    if chosen is None:
        # create_game's shortage message format, shared on purpose.
        raise ActionError(
            f"'{new_category.name}' only has {pool_size} usable question(s); "
            f"{game.questions_per_category} required."
        )
    # Cells CASCADE off the column, but the column survives — delete
    # explicitly. (Questions are PROTECT'd; only cells die here.)
    column.cells.all().delete()
    column.category = new_category
    column.save(update_fields=["category"])
    for row, question in enumerate(chosen):
        BoardCell.objects.create(
            game=game, column=column, question=question, row=row, value=_cell_value(game.mode, row)
        )
    return column


# --- Live mutations (Handoff #6 §F/§G, extended by Handoff #8 §F/§G) --------
# Every body the consumer runs lives here so the suite exercises exactly what
# the socket does.


def _clear_judgment(game: Game) -> list[str]:
    """Reset the §F judgment marker; returns the touched field names so
    callers can fold them into their own save(update_fields=...)."""
    game.judged_participant = None
    game.judged_correct = None
    return ["judged_participant", "judged_correct"]


@transaction.atomic
def open_cell(*, code: str, cell_id) -> Game:
    game = Game.objects.select_for_update().get(code=code)
    cell = game.cells.filter(pk=cell_id).first()
    if cell is None:
        raise ActionError("No such cell in this game.")
    if cell.state == CellState.ANSWERED:
        raise ActionError("That question was already played.")
    cell.state = CellState.OPEN
    cell.save(update_fields=["state"])
    game.current_cell = cell
    game.buzzer_open = False  # locked until the host finishes reading
    game.answer_revealed = False  # rule 5: a fresh cell is never pre-revealed
    fields = _clear_judgment(game)  # §F: a fresh cell carries no verdict
    game.save(update_fields=["current_cell", "buzzer_open", "answer_revealed", *fields])
    return game


def _perform_reveal(game: Game) -> None:
    """THE reveal body (rule 5): flips the persisted flag that feeds the
    snapshot's `revealed_answer` — the one sanctioned way an answer enters a
    snapshot. Shared verbatim by the host's explicit reveal action and §F's
    reveal-on-correct-judgment; no other code path may serialize an answer."""
    game.answer_revealed = True
    game.save(update_fields=["answer_revealed"])


@transaction.atomic
def reveal_answer(*, code: str) -> str:
    """Persist the reveal (feeds the snapshot's `revealed_answer` for polling
    boards) and return the answer text for the legacy WS `answer_reveal`
    event, which stays exactly as it was."""
    game = Game.objects.select_for_update().get(code=code)
    if game.current_cell_id is None:
        raise ActionError("No question is open.")
    _perform_reveal(game)
    # Fetch the question separately: select_for_update().select_related(
    # <nullable FK>) is a known Postgres foot-gun (see CHANGES.md).
    cell = BoardCell.objects.select_related("question").get(pk=game.current_cell_id)
    return cell.question.answer


@transaction.atomic
def set_buzzer(*, code: str, is_open: bool) -> Game:
    game = Game.objects.select_for_update().get(code=code)
    if game.current_cell_id is None:
        raise ActionError("Open a question first.")
    game.buzzer_open = is_open
    fields = ["buzzer_open"]
    if is_open:
        # §F: an explicit host reopen starts a fresh buzz round — the old
        # verdict banner leaves the screen. (Judge-wrong's automatic reopen
        # does NOT come through here, so its "✘ WRONG" stays visible.)
        fields += _clear_judgment(game)
    game.save(update_fields=fields)
    return game


# --- §F2 (Handoff #21): THUNDER FUCKED — the live flow ----------------------
# Stage machine, DERIVED entirely from persisted cell state (reload-proof,
# identical over WS and REST — the snapshot's `chug` block reads it in
# serializers.chug_state):
#   "fanfare"   open + not thunder_revealed        (sting; question WITHHELD)
#   "answering" thunder_revealed, unjudged         (race → wager → judge)
#   "pick"      judged CORRECT, no chugger yet     (winning phone picks)
#   "ready"     chugger set, clock not started     (host: "Start the clock")
#   "running"   thunder_started_at set             (screens count down)
# close_cell returns the block to null exactly as it clears current_cell.


def _thunder_cell(game: Game) -> BoardCell:
    """The open cell, which must be Thunder — the shared guard for the three
    host actions below. Fetched separately (the select_for_update +
    nullable-FK Postgres foot-gun, house rule)."""
    if game.current_cell_id is None:
        raise ActionError("No question is open.")
    cell = BoardCell.objects.get(pk=game.current_cell_id)
    if not cell.is_thunder:
        raise ActionError("This isn't a Thunder cell.")
    return cell


def _apply_chug(*, cell: BoardCell, chugger, credit) -> None:
    """C-4: the seconds ARE the stakes, replacing the cell's printed value.

    `chugger` drinks the wager (drinks_taken += wager); `credit` is the
    answering team on the CORRECT path (drinks_given + score += wager — the
    drinks-mode credit side, so the leaderboard reads exactly like a normal
    win of `wager` drinks) or None on the WRONG path (self-chug, credit to
    NOBODY — pinned). One DrinkAssignment row tells the history either way
    (self-chug rows point at themselves), and the §G once-per-cell marker
    is consumed here, which is how the drinks_already_assigned guard family
    extends to the chug."""
    wager = cell.thunder_wager
    DrinkAssignment.objects.create(
        cell=cell,
        from_participant=credit if credit is not None else chugger,
        to_participant=chugger,
        amount=wager,
    )
    chugger.drinks_taken += wager
    chugger.save(update_fields=["drinks_taken"])
    if credit is not None:
        credit.refresh_from_db()  # the chugger may BE the credit (self-pick)
        credit.drinks_given += wager
        credit.score += wager  # drinks dealt double as the leaderboard
        credit.save(update_fields=["drinks_given", "score"])
    cell.thunder_chugger = chugger
    cell.drinks_assigned = True
    cell.save(update_fields=["thunder_chugger", "drinks_assigned"])


@transaction.atomic
def thunder_reveal(*, code: str) -> Game:
    """C-5: fanfare → answering. The host screen auto-fires this when its
    sting playback ends (with a manual button fallback — the server never
    sleeps); the question text enters the payload and the buzzer OPENS for
    the sighted race (C-2)."""
    game = Game.objects.select_for_update().get(code=code)
    cell = _thunder_cell(game)
    if cell.thunder_revealed:
        raise ActionError("The question is already up.")
    cell.thunder_revealed = True
    cell.save(update_fields=["thunder_revealed"])
    game.buzzer_open = True
    fields = ["buzzer_open"] + _clear_judgment(game)  # a fresh race, no verdict
    game.save(update_fields=fields)
    return game


@transaction.atomic
def set_thunder_wager(*, code: str, seconds) -> Game:
    """C-2/C-3: the WAGER follows the buzz — the locked-in team shouts their
    chug seconds and the host (the referee) types them. Integer 3–30;
    re-typing pre-judgment is allowed (typo fix); judged cells are sealed."""
    game = Game.objects.select_for_update().get(code=code)
    cell = _thunder_cell(game)
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ActionError("Chug seconds must be a whole number.")
    if not (THUNDER_WAGER_MIN <= seconds <= THUNDER_WAGER_MAX):
        raise ActionError(
            f"Chug seconds run {THUNDER_WAGER_MIN}–{THUNDER_WAGER_MAX}."
        )
    if cell.answered_correctly is not None:
        raise ActionError("Already judged — the wager is locked in.")
    if not Buzz.objects.filter(cell=cell).exists():
        raise ActionError("The wager follows the buzz — wait for a team to buzz in.")
    cell.thunder_wager = seconds
    cell.save(update_fields=["thunder_wager"])
    return game


@transaction.atomic
def start_thunder_clock(*, code: str) -> Game:
    """C-5: ALWAYS host-manual — the room needs to be ready (and the song
    cued on the venue's own speakers; the app deliberately never plays it —
    §A.6). Stamps the server-side anchor every screen counts down from."""
    game = Game.objects.select_for_update().get(code=code)
    cell = _thunder_cell(game)
    if cell.thunder_chugger_id is None:
        raise ActionError("Nobody's holding a drink yet — judge the answer first.")
    if cell.thunder_started_at is not None:
        raise ActionError("The clock is already running.")
    cell.thunder_started_at = timezone.now()
    cell.save(update_fields=["thunder_started_at"])
    return game


@transaction.atomic
def mark_buzz_checked(*, code: str, participant) -> Game:
    """§F3 (Handoff #21): the lobby buzzer check — the player taps their
    buzzer in the lobby (their own sound plays LOCALLY on the phone; that's
    the point of the check) and this stamps the seat so the host lobby and
    the TV lobby can show the ✓. Lobby-only; a visual aid, never a start
    gate (C-7)."""
    game = Game.objects.select_for_update().get(code=code)
    if game.status != GameStatus.LOBBY:
        raise ActionError("The test smash is a lobby thing — the game already started.")
    if participant.role == ParticipantRole.HOST:
        raise ActionError("The host seat has no buzzer to check.")
    Participant.objects.filter(pk=participant.pk).update(buzz_checked_at=timezone.now())
    return game


@transaction.atomic
def reset_buzzer(*, code: str) -> Game:
    game = Game.objects.select_for_update().get(code=code)
    if game.current_cell_id is None:
        raise ActionError("No question is open.")
    # §F2 (#21), C-6: reset is the referee undo and works PRE-judgment only
    # on a Thunder cell — post-judgment the tallies already moved (the
    # wrong-answer self-chug applies instantly), and un-drinking is not a
    # thing. Pre-judgment it also clears the shouted wager (the wager
    # follows the buzz; no buzz, no wager). The question stays revealed —
    # un-revealing what the room already saw is theater (the remove_player
    # precedent) — so the stage stays "answering" and the host reopens the
    # buzzer for a fresh race.
    cell = BoardCell.objects.only("id", "is_thunder", "answered_correctly", "thunder_wager").get(
        pk=game.current_cell_id
    )
    if cell.is_thunder:
        if cell.answered_correctly is not None:
            raise ActionError("Thunder verdicts stick — the chug already counts. Close the cell to move on.")
        if cell.thunder_wager is not None:
            cell.thunder_wager = None
            cell.save(update_fields=["thunder_wager"])
    Buzz.objects.filter(cell_id=game.current_cell_id).delete()
    game.buzzer_open = False
    fields = ["buzzer_open"] + _clear_judgment(game)  # §F: fresh round, no verdict
    game.save(update_fields=fields)
    return game


@transaction.atomic
def register_buzz(*, code: str, participant) -> int:
    """A player's buzz; returns its 1-based order for the incremental event."""
    game = Game.objects.select_for_update().get(code=code)
    if not game.buzzer_open or game.current_cell_id is None:
        raise ActionError("Buzzer is locked.")
    if participant.role == ParticipantRole.HOST:
        raise ActionError("The host cannot buzz.")
    try:
        buzz = Buzz.objects.create(cell_id=game.current_cell_id, participant=participant)
    except IntegrityError:
        raise ActionError("You already buzzed.")
    # §F2 (#21), C-2: on a Thunder cell the FIRST buzz locks the buzzer for
    # real — the race decides THE team (their shout sets the wager) and
    # there are no steals, so later buzzes would be noise. Normal cells keep
    # accumulating an ordered list exactly as before.
    if BoardCell.objects.filter(pk=game.current_cell_id, is_thunder=True).exists():
        game.buzzer_open = False
        game.save(update_fields=["buzzer_open"])
    if game.judged_participant_id is not None:
        # §F: a NEW buzz supersedes the previous team's verdict on screen —
        # the board flips from "✘ WRONG — X" to the fresh buzz list.
        game.save(update_fields=_clear_judgment(game))
    return Buzz.objects.filter(cell_id=game.current_cell_id, created_at__lte=buzz.created_at).count()


@transaction.atomic
def judge_buzz(*, code: str, participant_id, correct: bool) -> Game:
    """§F: judging CORRECT also performs the reveal (same body as the host's
    explicit reveal — rule 5: no new answer path) and locks the buzzer;
    judging WRONG keeps the answer hidden (other teams are still in) and
    reopens the buzzer. Both set the judgment marker the snapshot carries as
    `current_cell.last_judgment`."""
    game = Game.objects.select_for_update().get(code=code)
    cell = game.current_cell
    if cell is None:
        raise ActionError("No question is open.")
    # §F (#11): a removed participant can't be judged — the buzz row they
    # rode in on was deleted at removal, so this only bites a stale click.
    participant = game.participants.filter(pk=participant_id, removed_at__isnull=True).first()
    if participant is None:
        raise ActionError("Unknown participant.")
    if cell.is_thunder:
        # §F2 (#21): the ONE big rule difference (C-2/C-6). Judging needs
        # the wager on the table first (the seconds ARE the stakes); a
        # CORRECT verdict hands the pick to the winning phone exactly like
        # a normal cell (stage "pick" — assign_drink applies the wager);
        # a WRONG verdict never reopens the buzzer (NO steals), reveals
        # the answer immediately (there's no steal to protect), and the
        # self-chug applies right here — the answering team drinks their
        # own shout, credit to nobody.
        if cell.thunder_wager is None:
            raise StructuredActionError(
                "Set the chug wager first — the buzzed-in team shouts their seconds.",
                "thunder_wager_required",
            )
        cell.answered_by = participant
        cell.answered_correctly = correct
        cell.save(update_fields=["answered_by", "answered_correctly"])
        game.buzzer_open = False
        game.save(update_fields=["buzzer_open"])
        if not game.answer_revealed:
            _perform_reveal(game)  # both verdicts show the answer on thunder
        if not correct:
            _apply_chug(cell=cell, chugger=participant, credit=None)
        game.judged_participant = participant
        game.judged_correct = correct
        game.save(update_fields=["judged_participant", "judged_correct"])
        return game
    if correct:
        cell.answered_by = participant
        cell.answered_correctly = True
        cell.save(update_fields=["answered_by", "answered_correctly"])
        if game.mode == GameMode.POINTS:
            participant.score += cell.value
            participant.save(update_fields=["score"])
        game.buzzer_open = False
        game.save(update_fields=["buzzer_open"])
        if not game.answer_revealed:
            _perform_reveal(game)  # §F: everyone sees the answer with the verdict
    else:
        if game.mode == GameMode.POINTS:
            participant.score -= cell.value
            participant.save(update_fields=["score"])
        # Wrong answer: that team is out of this round; reopen for others.
        # Deliberately NOT the explicit-reopen path — the verdict must stay.
        game.buzzer_open = True
        game.save(update_fields=["buzzer_open"])
    game.judged_participant = participant
    game.judged_correct = correct
    game.save(update_fields=["judged_participant", "judged_correct"])
    return game


@transaction.atomic
def assign_drink(*, code: str, actor, target_participant_id) -> Game:
    """§G: ONE drink assignment per cell, from either side.

    `actor` is the participant performing the action: the cell's winning
    player (the phone's `give_drink`) or the host's control seat (the panel's
    `assign_drinks` fallback — dead phone, distracted winner). Both consume
    the same `drinks_assigned` marker inside this transaction; first
    assignment wins. The credit always goes to the winner (drinks_given +
    score), whoever pressed the button. Any seat is a valid target — the
    HOST included, and self-assignment is allowed (a team drinking its own
    win is half the fun; to forbid it someday, add a
    `target.pk == cell.answered_by_id` check right below the target lookup)."""
    game = Game.objects.select_for_update().get(code=code)
    cell = game.current_cell
    if cell is None or cell.answered_by_id is None or not cell.answered_correctly:
        raise ActionError("Drinks can only be assigned after a correct answer.")
    if game.mode != GameMode.DRINKS:
        raise ActionError("This game is in points mode.")
    if actor.role != ParticipantRole.HOST and actor.pk != cell.answered_by_id:
        raise ActionError("Only the winning team can give the drink.")
    if cell.drinks_assigned:
        raise StructuredActionError(
            "Drinks were already assigned for this question.", "drinks_already_assigned"
        )
    # §F (#11): a removed seat is not a valid drink target.
    target = game.participants.filter(pk=target_participant_id, removed_at__isnull=True).first()
    if target is None:
        raise ActionError("Pick who drinks.")
    # (Self-assignment allowed; see docstring for the one-line forbid spot.)
    winner = cell.answered_by
    if cell.is_thunder:
        # §F2 (#21), C-4: on a Thunder cell the pick moves the WAGER, not
        # the printed value — same picker, same actors (winning phone or
        # host fallback), same once-per-cell marker (consumed inside
        # _apply_chug, which is what makes a second attempt land on the
        # drinks_already_assigned guard above). Stage → "ready".
        _apply_chug(cell=cell, chugger=target, credit=winner)
        return game
    DrinkAssignment.objects.create(
        cell=cell, from_participant_id=winner.pk, to_participant=target, amount=cell.value
    )
    target.drinks_taken += cell.value
    target.save(update_fields=["drinks_taken"])
    winner.refresh_from_db()  # target may BE the winner (self-assignment)
    winner.drinks_given += cell.value
    winner.score += cell.value  # drinks dealt double as the leaderboard
    winner.save(update_fields=["drinks_given", "score"])
    cell.drinks_assigned = True
    cell.save(update_fields=["drinks_assigned"])
    return game


@transaction.atomic
def remove_player(*, code: str, participant_id, actor) -> Game:
    """§F (Handoff #11): host kicks a player — the wrong-code joiner, the
    troll name, the table that left after round one.

    SOFT removal (Participant.removed_at; see the model comment for why hard
    deletes are off the table). Works in lobby AND active games; FINISHED
    games refuse (history is sealed). Open-cell interactions:

    - the removed player's Buzz rows for the CURRENTLY OPEN cell are deleted
      (their place in the live buzz order evaporates); historical buzzes on
      closed cells stay;
    - if the open cell's answered_by is the removed player and drinks are NOT
      yet assigned, the win is voided: answered_by/answered_correctly clear,
      the judgment marker clears, and the buzzer reopens — the round restarts
      for everyone else (the winner just vanished mid-celebration). If drinks
      WERE already assigned, everything stays: the drink happened, and the
      host closes the cell normally. (Deliberate: `answer_revealed` is left
      untouched either way — un-revealing what the room already saw is
      theater; the host can simply close the cell if the answer is up.)
    - if the removed player is the judgment marker's subject, the marker
      clears (the verdict banner would name someone who's gone).

    Already-removed → the documented StructuredActionError
    {"detail", "code": "player_removed"} — a NEW code (C4-safe) so the kicked
    phone can distinguish "you were kicked" from generic errors.
    """
    if actor.role != ParticipantRole.HOST:
        raise ActionError("Only the host can remove players.")
    game = Game.objects.select_for_update().get(code=code)
    if game.status == GameStatus.FINISHED:
        raise ActionError("The game is finished — its history is sealed.")
    participant = game.participants.filter(pk=participant_id).first()
    if participant is None:
        raise ActionError("Unknown participant.")
    if participant.role == ParticipantRole.HOST:
        raise ActionError("The host seat can't be removed.")
    if participant.removed_at is not None:
        raise StructuredActionError("That player was already removed.", "player_removed")

    participant.removed_at = timezone.now()
    participant.save(update_fields=["removed_at"])

    game_fields: list[str] = []
    if game.current_cell_id is not None:
        # Their place in the live buzz order evaporates; closed cells keep
        # their historical buzz rows.
        Buzz.objects.filter(cell_id=game.current_cell_id, participant_id=participant.pk).delete()
        # Fetch the cell separately (select_for_update + nullable-FK Postgres
        # foot-gun — house rule).
        cell = BoardCell.objects.get(pk=game.current_cell_id)
        if cell.answered_by_id == participant.pk and not cell.drinks_assigned:
            cell.answered_by = None
            cell.answered_correctly = None
            void_fields = ["answered_by", "answered_correctly"]
            # §F2 (#21): on a Thunder cell this void can only be stage
            # "pick" (judged correct, drink not yet assigned — the wrong
            # path consumes drinks_assigned instantly). The wager was the
            # vanished team's shout, so it goes with them; the round
            # restarts in "answering" with the buzzer reopened below.
            if cell.is_thunder and cell.thunder_wager is not None:
                cell.thunder_wager = None
                void_fields.append("thunder_wager")
            cell.save(update_fields=void_fields)
            game.buzzer_open = True  # the round restarts for everyone else
            game_fields.append("buzzer_open")
            game_fields += _clear_judgment(game)
    if game.judged_participant_id == participant.pk and "judged_participant" not in game_fields:
        game_fields += _clear_judgment(game)
    if game_fields:
        game.save(update_fields=game_fields)
    return game


@transaction.atomic
def close_cell(*, code: str) -> Game:
    game = Game.objects.select_for_update().get(code=code)
    cell = game.current_cell
    if cell is None:
        raise ActionError("No question is open.")
    cell.state = CellState.ANSWERED
    cell.save(update_fields=["state"])
    game.current_cell = None
    game.buzzer_open = False
    game.answer_revealed = False  # rule 5: reveal never outlives its cell
    fields = _clear_judgment(game)  # §F: the verdict never outlives its cell
    game.save(update_fields=["current_cell", "buzzer_open", "answer_revealed", *fields])
    return game


@transaction.atomic
def finalize_game(*, code: str) -> Game:
    """finish_game: mark finished and compute + persist the outcome (§G1).

    Winner = highest score among role=player participants, ties included, in
    BOTH modes. Documented drinks-mode reading: `score` is the credit side —
    assign_drink adds cell.value to the winner's score when drinks are
    assigned ("drinks dealt double as the leaderboard"), so highest score ==
    most drinks dealt; `drinks_taken` is the penalty side and never decides
    the winner. A finished game with no players stores no winners; an
    abandoned game never reaches this function, so it has no outcome at all.
    """
    game = Game.objects.select_for_update().get(code=code)
    if game.status == GameStatus.FINISHED:
        return game  # idempotent: a second finish_game must not rewrite history
    game.status = GameStatus.FINISHED
    game.finished_at = timezone.now()
    game.buzzer_open = False
    game.answer_revealed = False
    fields = _clear_judgment(game)
    game.save(update_fields=["status", "finished_at", "buzzer_open", "answer_revealed", *fields])
    # §F (#11): removed players can't tie for the crown — their tallies stay
    # visible only in the host-private report (which keeps ALL participants).
    players = list(game.participants.filter(role=ParticipantRole.PLAYER, removed_at__isnull=True))
    if players:
        top = max(p.score for p in players)
        game.winners.set([p for p in players if p.score == top])
    return game


# --- §I (Handoff #13): tournament standings + advancement -------------------


def ranked_players(game) -> list[tuple]:
    """§F1a (Handoff #20): the ONE ranking computation, now returning the
    Participant rows themselves — [(participant, rank), ...] best first —
    so advance_round can link advancers to the seats that earned them.
    Everything §I2 documented is unchanged and lives here: ACTIVE players
    only (role=player, removed excluded), the SAME truths finalize_game
    uses (the documented drinks-mode reading: score is the credit side, so
    highest score == most drinks dealt), competition ranking (1, 1, 3) —
    ties share a rank, exactly as ties share the winners crown. Filters the
    PREFETCHED participants in Python (the get_participants convention) so
    detail views pay no extra query. INTERNAL: participants never leave the
    server through this — public callers go through game_standings below."""
    players = [
        p for p in game.participants.all()
        if p.role == ParticipantRole.PLAYER and p.removed_at is None
    ]
    players.sort(key=lambda p: (-p.score, p.name))
    ranked = []
    prev_score = None
    prev_rank = 0
    for position, player in enumerate(players, start=1):
        rank = prev_rank if player.score == prev_score else position
        ranked.append((player, rank))
        prev_score, prev_rank = player.score, rank
    return ranked


def game_standings(game) -> list[dict]:
    """§I2: {name, score, rank} rows for one game's ACTIVE players, best
    first — the PUBLIC projection of ranked_players above, and the pinned
    shape for every tournament surface (rule 5: names + scores only; no
    question content, no participant ids, no tokens)."""
    return [
        {"name": player.name, "score": player.score, "rank": rank}
        for player, rank in ranked_players(game)
    ]


@transaction.atomic
def advance_round(*, tournament, round_number: int, per_game: int) -> list:
    """§I2: compute + persist who goes through from round N — host-CONFIRMED
    (this only runs when the host presses Advance), server-COMPUTED (rule 4).

    `per_game` (1 or 2 — "winners and/or 2nd") means everyone with rank <=
    per_game in their game advances; a tie at a qualifying rank advances
    everyone in it (ties are real, exactly like Game.winners).

    Chosen semantics, pinned by tests: RE-RUNNABLE, not merely idempotent —
    a second call REPLACES the round's advancers wholesale (delete + rewrite
    inside this transaction), so a host can change their mind (top-1 →
    top-2) before round N+1 starts. The row lock on the tournament
    serializes concurrent calls; the caller still catches IntegrityError
    OUTSIDE this atomic block (the house rule — this mutation is exactly the
    kind of place that foot-gun bites).

    Rejections (StructuredActionError, new C4-pinned codes):
      - a round with no games        → "tournament_round_empty"
      - any round game not finished  → "tournament_round_incomplete"
    """
    from .models import Tournament, TournamentAdvancer  # local: keeps module import order simple

    locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
    games = list(
        locked.games.filter(round_number=round_number)
        .order_by("created_at", "id")
        .prefetch_related("participants")
    )
    if not games:
        raise StructuredActionError(
            f"Round {round_number} has no games yet.", "tournament_round_empty"
        )
    unfinished = [g.code for g in games if g.status != GameStatus.FINISHED]
    if unfinished:
        raise StructuredActionError(
            f"Round {round_number} isn't finished — still playing: {', '.join(sorted(unfinished))}.",
            "tournament_round_incomplete",
        )
    prior_targets = {
        (a.source_game_id, a.name): a.target_game_id
        for a in TournamentAdvancer.objects.filter(
            tournament=locked, round_number=round_number, target_game__isnull=False
        )
    }
    TournamentAdvancer.objects.filter(tournament=locked, round_number=round_number).delete()
    # §F1a (#20): rows now carry the SEAT that earned them (ranked_players,
    # the same computation game_standings projects) — the linkage the claim
    # endpoint verifies. Names stay what advances (the #13 ruling, upgraded).
    rows = [
        TournamentAdvancer(
            tournament=locked,
            round_number=round_number,
            name=player.name,
            source_game=game,
            rank=rank,
            source_participant=player,
        )
        for game in games
        for player, rank in ranked_players(game)
        if rank <= per_game
    ]
    # §F1b/C-4: exactly one next-round game already there → every advancer
    # auto-targets it, set BEFORE the insert (one write, and a re-run
    # re-derives it — replaced rows would otherwise come back targetless).
    # Multi-game next round: a re-run PRESERVES the host's routing for rows
    # that survive it, matched by (source_game, name) — "top-1 → top-2"
    # must not force re-routing the winner who was already assigned; only
    # genuinely new qualifiers arrive unrouted.
    next_games = list(locked.games.filter(round_number=round_number + 1))
    if len(next_games) == 1:
        for row in rows:
            row.target_game = next_games[0]
    elif prior_targets:
        for row in rows:
            row.target_game_id = prior_targets.get((row.source_game_id, row.name))
    TournamentAdvancer.objects.bulk_create(rows)
    return rows


def _auto_target_advancers(*, tournament, next_round: int) -> None:
    """§F1b (Handoff #20), the CREATION side of C-4's gap-closer: called
    after a tournament game is created in `next_round`. If that round now
    has exactly ONE game, every round-(N-1) advancer targets it (the common
    flow: host advances first, builds the next board second). The moment a
    SECOND game appears in the round, single-game auto-targets stop being
    meaningful — C-4 says multi-game rounds are host-assigned — so targets
    pointing into the round are CLEARED and the console's per-advancer
    selector takes over (flagged ruling C-4a in CHANGES: qualifiers see
    "ask your host" until assigned, which beats phones claiming into a game
    the host is about to split)."""
    from .models import TournamentAdvancer  # local, matching advance_round

    round_games = list(tournament.games.filter(round_number=next_round))
    prior = TournamentAdvancer.objects.filter(tournament=tournament, round_number=next_round - 1)
    if len(round_games) == 1:
        prior.update(target_game=round_games[0])
    elif len(round_games) == 2:
        # Exactly the 1→2 transition: whatever targets exist were the
        # single-game AUTO ones, now stale. Rounds already at 2+ games are
        # host-managed — adding a 4th board must not wipe manual routing.
        prior.filter(target_game__round_number=next_round).update(target_game=None)
