"""§F1e (Handoff #18): the board backup email, sent to the HOST after a
successful game create.

Same iron rule as accounts/emails.py: SENDING MUST NEVER BREAK THE ACTION —
the one send body catches Exception, logs a warning and returns, so an email
outage can never turn a successful create into a 500. Called from the VIEW
(GameCreateView), deliberately NOT from services.create_game: the service is
@transaction.atomic and the suite calls it directly hundreds of times — the
view placement means the mail goes out only after the transaction actually
committed, and only on the real HTTP path.

Content policy (rule 5 adjacent): this email carries questions AND answers.
That is fine — it goes to the HOST's account address only, and the host
already sees every answer through the host-private board/answer endpoints.
It must NEVER be sent to players (players have no email here anyway).

This email IS the v1 "Game Backup" until the §F9 PDF exists — its footer
says so.
"""
import logging

from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def build_board_email_body(game) -> str:
    """Plain-text board dump: code, mode, created stamp, then per column the
    category name and each row's value + question + answer. Kept separate
    from the send so tests can pin the content without touching the mailer.
    """
    lines = [
        f"Your Drinkquiry board is ready — game {game.code}.",
        "",
        f"Mode: {game.get_mode_display()}",
        f"Created: {game.created_at:%Y-%m-%d %H:%M} UTC",
        f"Questions per category: {game.questions_per_category}",
        "",
    ]
    unit = "🍺" if game.mode == "drinks" else "pts"
    for column in game.columns.all().select_related("category").prefetch_related("cells__question"):
        lines.append(f"=== {column.category.name} ===")
        for cell in column.cells.all():
            q = cell.question
            lines.append(f"  [{cell.value} {unit}] {q.question_text}")
            lines.append(f"      → {q.answer}")
        lines.append("")
    lines += [
        "— Drinkquiry",
        "",
        "This email is your game backup — keep it in case of technical",
        "difficulties on game night.",
    ]
    return "\n".join(lines)


def send_board_email(game) -> None:
    """Email the full board (questions + answers) to the game's host.

    Warn-and-continue on ANY failure (the accounts/emails.py contract):
    the create already succeeded; the email is a convenience on top.
    """
    try:
        send_mail(
            f"Your Drinkquiry board — game {game.code}",
            build_board_email_body(game),
            None,  # → DEFAULT_FROM_EMAIL
            [game.host.email],
        )
    except Exception:  # noqa: BLE001 — deliberately broad; see module docstring
        logger.warning("Board email send failed (game=%s)", game.code, exc_info=True)
