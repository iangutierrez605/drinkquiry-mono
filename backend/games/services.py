"""Game construction + live-mutation logic, kept out of views/consumers for
testability. The WebSocket consumer wraps the mutation functions below with
`database_sync_to_async`; `games/tests.py` calls them directly."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from trivia.models import Category, ModerationStatus, Question, Visibility

from .models import BoardCell, BoardColumn, CellState, Game, GameMode, GameStatus, ParticipantRole


class ActionError(Exception):
    """A host/player action that can't be applied; the consumer relays the
    message to the sender as an {type: "error"} event."""

PUBLIC_APPROVED = Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)


def usable_questions(category: Category, user):
    """Questions this user is allowed to put on a board from this category."""
    allowed = PUBLIC_APPROVED | Q(owner__isnull=True)
    if user and user.is_authenticated:
        allowed = allowed | Q(owner=user)
    return category.questions.filter(allowed)


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
        qs = list(usable_questions(category, host).order_by("difficulty", "?")[: questions_per_category * 3])
        if len(qs) < questions_per_category:
            shortages[category.name] = len(qs)
        else:
            # Sort chosen subset easiest->hardest so value scales with row.
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


# --- Live host mutations (Handoff #6 §F/§G) --------------------------------
# Only the actions §F/§G touch live here (open/reveal/close/finish); the rest
# (buzzer, judging, drinks) stay in the consumer as before.


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
    game.save(update_fields=["current_cell", "buzzer_open", "answer_revealed"])
    return game


@transaction.atomic
def reveal_answer(*, code: str) -> str:
    """Persist the reveal (feeds the snapshot's `revealed_answer` for polling
    boards) and return the answer text for the legacy WS `answer_reveal`
    event, which stays exactly as it was."""
    game = Game.objects.select_for_update().get(code=code)
    if game.current_cell_id is None:
        raise ActionError("No question is open.")
    game.answer_revealed = True
    game.save(update_fields=["answer_revealed"])
    # Fetch the question separately: select_for_update().select_related(
    # <nullable FK>) is a known Postgres foot-gun (see CHANGES.md).
    cell = BoardCell.objects.select_related("question").get(pk=game.current_cell_id)
    return cell.question.answer


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
    game.save(update_fields=["current_cell", "buzzer_open", "answer_revealed"])
    return game


@transaction.atomic
def finalize_game(*, code: str) -> Game:
    """finish_game: mark finished and compute + persist the outcome (§G1).

    Winner = highest score among role=player participants, ties included, in
    BOTH modes. Documented drinks-mode reading: `score` is the credit side —
    the consumer adds cell.value to the answerer's score when drinks are
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
    game.save(update_fields=["status", "finished_at", "buzzer_open", "answer_revealed"])
    players = list(game.participants.filter(role=ParticipantRole.PLAYER))
    if players:
        top = max(p.score for p in players)
        game.winners.set([p for p in players if p.score == top])
    return game
