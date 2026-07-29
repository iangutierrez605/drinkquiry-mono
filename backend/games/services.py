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


def usable_questions(category: Category, user):
    """Questions this user is allowed to put on a board from this category."""
    allowed = PUBLIC_APPROVED | Q(owner__isnull=True)
    if user and user.is_authenticated:
        allowed = allowed | Q(owner=user)
    return category.questions.filter(allowed)


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


@transaction.atomic
def create_game(*, host, mode: str, category_ids: list[int], questions_per_category: int) -> Game:
    if not (1 <= questions_per_category <= 10):
        raise ValidationError({"questions_per_category": "Must be between 1 and 10."})
    if not (1 <= len(category_ids) <= 8):
        raise ValidationError({"categories": "Pick between 1 and 8 categories."})
    if len(set(category_ids)) != len(category_ids):
        raise ValidationError({"categories": "Duplicate categories are not allowed."})

    categories = list(Category.objects.filter(id__in=category_ids))
    found = {c.id for c in categories}
    if missing := set(category_ids) - found:
        raise ValidationError({"categories": f"Unknown category ids: {sorted(missing)}."})

    # Requirement 4: refuse to create the game if any chosen category is short
    # on questions the host may use.
    shortages = {}
    picked: dict[int, list[Question]] = {}
    for category in categories:
        # §J2: difficulty stays the primary key (same difficulty-slot behavior
        # as before), and WITHIN a difficulty the host's unused questions come
        # first, then least-used, then random. The 3x fetch window and the
        # stable easiest->hardest sort below are unchanged, so the shortage
        # contract and row/value placement behave exactly as they did.
        qs = list(
            _preference_ordered(usable_questions(category, host), host).order_by(
                "difficulty", "host_uses", "global_uses", "?"
            )[: questions_per_category * 3]
        )
        if len(qs) < questions_per_category:
            shortages[category.name] = len(qs)
        else:
            # Sort chosen subset easiest->hardest so value scales with row.
            # Python's sort is stable, so the §J2 preference order within each
            # difficulty survives the re-sort.
            chosen = sorted(qs, key=lambda q: q.difficulty)[:questions_per_category]
            picked[category.id] = chosen
    if shortages:
        raise ValidationError(
            {
                "categories": [
                    f"'{name}' only has {count} usable question(s); {questions_per_category} required."
                    for name, count in shortages.items()
                ]
            }
        )

    game = Game.objects.create(host=host, mode=mode, questions_per_category=questions_per_category)

    ordered = {c.id: c for c in categories}
    for position, category_id in enumerate(category_ids):
        column = BoardColumn.objects.create(game=game, category=ordered[category_id], position=position)
        for row, question in enumerate(picked[category_id]):
            value = (row + 1) if mode == GameMode.DRINKS else (row + 1) * 100
            BoardCell.objects.create(game=game, column=column, question=question, row=row, value=value)
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


@transaction.atomic
def reset_buzzer(*, code: str) -> Game:
    game = Game.objects.select_for_update().get(code=code)
    if game.current_cell_id is None:
        raise ActionError("No question is open.")
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
    participant = game.participants.filter(pk=participant_id).first()
    if participant is None:
        raise ActionError("Unknown participant.")
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
    target = game.participants.filter(pk=target_participant_id).first()
    if target is None:
        raise ActionError("Pick who drinks.")
    # (Self-assignment allowed; see docstring for the one-line forbid spot.)
    winner = cell.answered_by
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
    players = list(game.participants.filter(role=ParticipantRole.PLAYER))
    if players:
        top = max(p.score for p in players)
        game.winners.set([p for p in players if p.score == top])
    return game
