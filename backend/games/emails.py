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

§F5 (Handoff #21): the email grew up — the plain-text part STAYS exactly
as it was (it is the every-client fallback and the pinned content), an HTML
alternative gives it clean per-category sections instead of a wall of text,
and the crib-sheet PDF (games/pdf.py, C-9's layout, Thunder cells marked)
rides along as `drinkquiry-<CODE>.pdf`. A PDF build failure downgrades to
the email without the attachment — never breaks the send, which never
breaks the create (the iron rule, twice).
"""
import logging

from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape

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


def build_board_email_html(game) -> str:
    """§F5c (#21): the cleaned HTML body — header block, one section per
    category in board order, each question with its value and answer,
    Thunder cells marked. Inline styles only (email clients); host-facing
    voice per §A.4: playful title line, clean scannable body. The plain-text
    part (build_board_email_body above) is untouched and remains the
    content the suite pins."""
    unit = "🍺" if game.mode == "drinks" else "pts"
    sections = []
    for column in game.columns.all().select_related("category").prefetch_related("cells__question"):
        rows = []
        for cell in column.cells.all():
            question = cell.question
            thunder = ""
            if cell.is_thunder:
                thunder = (
                    '<div style="color:#8a5a00;font-weight:bold;margin-top:10px;">'
                    "⚡ THUNDER FUCKED — chug wager replaces this cell's value</div>"
                )
            rows.append(
                f"{thunder}"
                '<div style="margin:6px 0 2px;">'
                f"<strong>[{cell.value} {unit}]</strong> {escape(question.question_text)}</div>"
                '<div style="margin:0 0 8px 20px;color:#155e2b;">'
                f"→ <strong>{escape(question.answer)}</strong></div>"
            )
        sections.append(
            '<h3 style="margin:18px 0 4px;border-bottom:1px solid #d8dee9;'
            f'padding-bottom:3px;">{escape(column.category.name)}</h3>' + "".join(rows)
        )
    return (
        '<div style="font-family:Helvetica,Arial,sans-serif;color:#0d1622;'
        'max-width:640px;margin:0 auto;">'
        '<h2 style="margin:0 0 2px;">Your board is ready 🍻</h2>'
        '<p style="margin:0;color:#42506b;">'
        f"Game <strong>{escape(game.code)}</strong> · {escape(game.get_mode_display())} mode · "
        f"created {game.created_at:%Y-%m-%d %H:%M} UTC · "
        f"{game.questions_per_category} per category</p>"
        + "".join(sections)
        + '<p style="color:#42506b;margin-top:16px;">This email is your game backup — '
        "the attached PDF is the same board as a printable crib sheet. Keep both "
        "host-side: they carry every answer.</p></div>"
    )


def send_board_email(game) -> None:
    """Email the full board (questions + answers) to the game's host.

    Warn-and-continue on ANY failure (the accounts/emails.py contract):
    the create already succeeded; the email is a convenience on top. The
    PDF has its own inner guard — a reportlab hiccup downgrades to the
    email without the attachment rather than killing the send.
    """
    try:
        message = EmailMultiAlternatives(
            f"Your Drinkquiry board — game {game.code}",
            build_board_email_body(game),
            None,  # → DEFAULT_FROM_EMAIL
            [game.host.email],
        )
        message.attach_alternative(build_board_email_html(game), "text/html")
        try:
            from .pdf import build_crib_sheet_pdf

            pdf_bytes = build_crib_sheet_pdf(game)
        except Exception:  # noqa: BLE001 — the attachment is optional, the email is not
            logger.warning("Crib-sheet PDF build failed (game=%s)", game.code, exc_info=True)
            pdf_bytes = None
        if pdf_bytes:
            message.attach(f"drinkquiry-{game.code}.pdf", pdf_bytes, "application/pdf")
        message.send()
    except Exception:  # noqa: BLE001 — deliberately broad; see module docstring
        logger.warning("Board email send failed (game=%s)", game.code, exc_info=True)
