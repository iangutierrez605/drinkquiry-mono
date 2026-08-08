"""§F5 (Handoff #21): the host crib-sheet PDF — C-9's layout.

One flowing Letter document, built by reportlab (the ONE sanctioned new
backend dep, pinned in requirements.txt): a header block (game code, mode,
date, board size), then one section per category IN BOARD ORDER, each
question with its answer and value, Thunder cells MARKED (⚡ + "chug wager")
— the host may read in order and should know what's coming.

Pure function of the game → unit-testable (games.tests.CribSheetTests
asserts the bytes open, page count >= 1, and a light text extraction finds
the code/questions/markers — no pypdf; reportlab writes, the test reads).

Voice (§A.4): this is HOST-facing — playful title line, CLEAN scannable
body. The ⚡ glyph itself needs a Unicode font: DejaVu Sans is registered
when present (the Dockerfile installs fonts-dejavu-core so the deployed
image has it; the sandbox does too), with a text-only "**" fallback so a
font-less environment still ships a correct, just less sparky, PDF — the
words "THUNDER FUCKED — chug wager" carry the meaning either way.

Content policy: exactly the board-backup email's (games/emails.py) — this
carries questions AND answers, goes to the HOST only, and never to players.
"""
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

# Common DejaVu locations (Debian/Ubuntu package fonts-dejavu-core first —
# that's what the backend Dockerfile installs).
_DEJAVU_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
)
_SYMBOL_FONT_STATE: list = []  # lazy singleton: [name | None]


def _symbol_font() -> str | None:
    """Register DejaVu Sans once if available; return its font name, else
    None (callers fall back to a plain-text marker)."""
    if not _SYMBOL_FONT_STATE:
        name = None
        for path in _DEJAVU_CANDIDATES:
            if os.path.exists(path):
                try:
                    pdfmetrics.registerFont(TTFont("DejaVuSans", path))
                    name = "DejaVuSans"
                    break
                except Exception:  # noqa: BLE001 — a broken font file must not break the PDF
                    continue
        _SYMBOL_FONT_STATE.append(name)
    return _SYMBOL_FONT_STATE[0]


def thunder_marker() -> str:
    """The ⚡ (or fallback) markup fragment, shared by every thunder line."""
    font = _symbol_font()
    if font:
        return f'<font name="{font}">\u26a1</font>'
    return "**"


def build_crib_sheet_pdf(game) -> bytes:
    """Render C-9's crib sheet for `game` and return the PDF bytes."""
    styles = {
        "title": ParagraphStyle(
            "title", fontName="Helvetica-Bold", fontSize=20, leading=24,
            textColor=colors.HexColor("#0d1622"), spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "meta", fontName="Helvetica", fontSize=10.5, leading=14,
            textColor=colors.HexColor("#42506b"), spaceAfter=2,
        ),
        "category": ParagraphStyle(
            "category", fontName="Helvetica-Bold", fontSize=14, leading=18,
            textColor=colors.HexColor("#0d1622"), spaceBefore=14, spaceAfter=4,
        ),
        "question": ParagraphStyle(
            "question", fontName="Helvetica", fontSize=10.5, leading=14,
            spaceBefore=5, leftIndent=0,
        ),
        "answer": ParagraphStyle(
            "answer", fontName="Helvetica", fontSize=10.5, leading=14,
            leftIndent=22, textColor=colors.HexColor("#155e2b"), spaceAfter=1,
        ),
        "thunder": ParagraphStyle(
            "thunder", fontName="Helvetica-Bold", fontSize=10.5, leading=14,
            textColor=colors.HexColor("#8a5a00"), spaceBefore=6,
        ),
    }

    unit = "drinks" if game.mode == "drinks" else "pts"
    columns = list(
        game.columns.all().select_related("category").prefetch_related("cells__question")
    )
    cell_count = sum(len(column.cells.all()) for column in columns)

    story = [
        Paragraph("Your night, on one sheet \U0001f37b", styles["title"]),
        Paragraph(
            f"Game <b>{escape(game.code)}</b> · {escape(game.get_mode_display())} mode · "
            f"created {game.created_at:%Y-%m-%d %H:%M} UTC",
            styles["meta"],
        ),
        Paragraph(
            f"Board: {len(columns)} categor{'y' if len(columns) == 1 else 'ies'} × "
            f"{game.questions_per_category} question"
            f"{'' if game.questions_per_category == 1 else 's'} ({cell_count} cells)",
            styles["meta"],
        ),
        Spacer(1, 0.08 * inch),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#d8dee9")),
    ]

    marker = thunder_marker()
    for column in columns:
        story.append(Paragraph(escape(column.category.name), styles["category"]))
        for cell in column.cells.all():
            question = cell.question
            if cell.is_thunder:
                # C-9: the host should KNOW. The marker leads the question so
                # a read-in-order host sees it before opening their mouth.
                story.append(
                    Paragraph(
                        f"{marker} <b>THUNDER FUCKED</b> — chug wager replaces the "
                        f"{cell.value}-{unit} value on this one",
                        styles["thunder"],
                    )
                )
            story.append(
                Paragraph(
                    f"<b>[{cell.value} {unit}]</b> {escape(question.question_text)}",
                    styles["question"],
                )
            )
            story.append(Paragraph(f"\u2192 <b>{escape(question.answer)}</b>", styles["answer"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e6eaf2")))

    story.append(Spacer(1, 0.12 * inch))
    story.append(
        Paragraph(
            "Keep this sheet host-side — it has every answer. Thunder cells: sting plays, "
            "question shows, teams race, the winner shouts their chug seconds (3\u201330), "
            "you type it and judge. Cue the song on your own speakers.",
            styles["meta"],
        )
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        title=f"Drinkquiry crib sheet — game {game.code}",
        author="Drinkquiry",
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
    )
    document.build(story)
    return buffer.getvalue()
