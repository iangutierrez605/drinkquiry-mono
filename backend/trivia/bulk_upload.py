"""Bulk question upload — CSV/zip parsing and validation (Handoff #4 §G, #5 §F/§G).

Pure-ish helpers: `parse_bulk_upload()` turns an uploaded file (a plain CSV or
a zip whose root CSV references media files inside the archive) into either a
file-level error, or per-row errors + a list of ready-to-create row dicts +
skipped duplicate row numbers + the set of categories that would be
auto-created. The view owns quota checks, the transaction, and response shapes.

Row numbers everywhere are 1-based *including the header* (first data row = 2),
i.e. what the user sees in their spreadsheet app. We number by CSV record, not
physical line, so multi-line quoted cells don't skew the count.

Media files are validated at parse time (existence, uncompressed size, type via
the SAME validators the single-question form uses) but only *read and saved* in
`create_rows`, after every row has validated — the parse-then-create split is
what makes all-or-nothing hold without storage cleanup on rollback. Note: a
media file referenced by several rows is stored once per row (Django storage
suffixes duplicate names) — dedupe by hash is a later optimization.
"""
import csv
import io
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db.models import Q

from .models import Category, MediaType, ModerationStatus, Question, Visibility
from .images import check_image_pixels
from .validators import validate_audio, validate_image, validate_video

MAX_ROWS = 500
MAX_CSV_BYTES = 1 * 1024 * 1024          # 1 MB — the CSV (or the zip's CSV member)
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024    # 200 MB compressed
MAX_BYTES = MAX_CSV_BYTES                # historical alias (Handoff #4 name)
REQUIRED_COLUMNS = ("category", "question_text", "answer", "difficulty", "visibility")
MEDIA_COLUMNS = ("media_type", "media_file")  # optional; zip format only
MAX_ANSWER_LEN = Question._meta.get_field("answer").max_length  # 500

# media_type value -> (Question field name, validator)
MEDIA_FIELDS = {
    MediaType.IMAGE: ("image", validate_image),
    MediaType.AUDIO: ("audio", validate_audio),
    MediaType.VIDEO: ("video", validate_video),
}

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass
class ParseResult:
    file_error: str | None = None            # file-level problem → plain {"detail"} 400
    errors: list[dict] = field(default_factory=list)   # [{"row", "field", "message"}]
    rows: list[dict] = field(default_factory=list)     # validated, ready to create
    skipped: list[int] = field(default_factory=list)   # duplicate row numbers
    # §F: categories to auto-create — casefolded key -> stored name (first
    # row's exact casing wins). Empty unless create_categories was requested.
    new_categories: dict[str, str] = field(default_factory=dict)
    media_files: int = 0                     # §G: rows whose media would be saved
    media_bytes_total: int = 0               # §F3: summed uncompressed bytes of that media
    archive: zipfile.ZipFile | None = None   # open zip (view reads media at create time)

    @property
    def category_names(self) -> list[str]:
        return sorted(self.new_categories.values())


class _ArchiveMember:
    """Duck-typed stand-in so trivia/validators.py can vet a zip member
    (uncompressed size + extension-derived content type) before extraction."""

    def __init__(self, info: zipfile.ZipInfo):
        self.size = info.file_size
        self.content_type = mimetypes.guess_type(info.filename)[0]


def _unsafe_member_name(name: str) -> bool:
    """Zip-slip guard: absolute paths, drive prefixes, or `..` segments."""
    if name.startswith(("/", "\\")) or _DRIVE_PREFIX.match(name):
        return True
    return any(part == ".." for part in re.split(r"[\\/]", name))


def eligible_categories(user, official: bool):
    """Categories the uploader may add questions to.

    Same rule as Category.accepts_questions_from (official | own | publicly
    visible), expressed as a queryset; official uploads match official
    categories only. §F5: deleted categories are never eligible — a row whose
    name only matches a deleted category reads as "no such category", so
    with create_categories on, a FRESH one is created (the partial unique
    constraint permits the name reuse).
    """
    qs = Category.objects.filter(deleted_at__isnull=True)
    if official:
        return qs.filter(owner__isnull=True)
    return qs.filter(
        Q(owner__isnull=True)
        | Q(owner=user)
        | Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)
    ).distinct()


def _pick_category(name, candidates, user):
    """Pick one Category from same-name candidates.

    Prefer the uploader's own, then official, then other public ones; a tie
    inside the winning tier is ambiguous → (None, msg). Ambiguity stays an
    error even with create_categories (§F: never silently create a duplicate
    of something that already matches).
    """
    for tier in (
        [c for c in candidates if c.owner_id == user.id],
        [c for c in candidates if c.owner_id is None],
        candidates,
    ):
        if len(tier) == 1:
            return tier[0], None
        if len(tier) > 1:
            return None, f'"{name}" matches more than one category you can use — rename yours to disambiguate.'
    return None, f'No category named "{name}" that you can add questions to.'  # unreachable


def parse_bulk_upload(uploaded, user, *, official: bool, skip_duplicates: bool,
                      create_categories: bool = False) -> ParseResult:
    """Entry point. Detects zip vs plain CSV by content (`zipfile.is_zipfile`),
    never by filename. Plain CSVs behave exactly as v1 when the new flags/
    columns are absent."""
    if zipfile.is_zipfile(uploaded):
        uploaded.seek(0)
        return _parse_zip(uploaded, user, official=official,
                          skip_duplicates=skip_duplicates, create_categories=create_categories)
    uploaded.seek(0)
    return _parse_plain_csv(uploaded, user, official=official,
                            skip_duplicates=skip_duplicates, create_categories=create_categories)


# Historical alias — Handoff #4 name, same behavior for plain CSVs.
parse_bulk_csv = parse_bulk_upload


def _parse_plain_csv(uploaded, user, **opts) -> ParseResult:
    result = ParseResult()
    if uploaded.size > MAX_CSV_BYTES:
        result.file_error = "File is too large — the limit is 1 MB (up to 500 rows)."
        return result
    try:
        text = uploaded.read().decode("utf-8-sig")  # tolerate a BOM
    except UnicodeDecodeError:
        result.file_error = "Couldn't read the file as UTF-8 text. Export it as CSV (UTF-8) and try again."
        return result
    _parse_csv_text(text, user, result=result, members=None, archive=None, **opts)
    return result


def _parse_zip(uploaded, user, **opts) -> ParseResult:
    result = ParseResult()
    if uploaded.size > MAX_ARCHIVE_BYTES:
        result.file_error = "Archive is too large — the limit is 200 MB."
        return result
    try:
        zf = zipfile.ZipFile(uploaded)
    except zipfile.BadZipFile:
        result.file_error = "Couldn't read the file as a zip archive."
        return result

    members: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if _unsafe_member_name(info.filename):
            result.file_error = f'Unsafe path in archive: "{info.filename}". Paths must be relative, without "..".'
            return result
        if not info.is_dir():
            members[info.filename] = info

    root_csvs = [n for n in members if "/" not in n and n.lower().endswith(".csv")]
    if len(root_csvs) != 1:
        if len(root_csvs) > 1:
            result.file_error = "The archive must contain exactly one CSV at its root (found several)."
        elif any(n.lower().endswith(".csv") for n in members):
            result.file_error = (
                "The CSV must sit at the archive root, not inside a folder — "
                "zip the files themselves, not the folder containing them."
            )
        else:
            result.file_error = "The archive doesn't contain a CSV file at its root."
        return result

    csv_name = root_csvs[0]
    csv_info = members.pop(csv_name)  # the CSV itself can't be referenced as media
    if csv_info.file_size > MAX_CSV_BYTES:
        result.file_error = "The CSV inside the archive is too large — the limit is 1 MB (up to 500 rows)."
        return result
    try:
        text = zf.read(csv_name).decode("utf-8-sig")
    except UnicodeDecodeError:
        result.file_error = "Couldn't read the archive's CSV as UTF-8 text. Export it as CSV (UTF-8) and try again."
        return result

    _parse_csv_text(text, user, result=result, members=members, archive=zf, **opts)
    result.archive = zf
    return result


def _parse_csv_text(text, user, *, official: bool, skip_duplicates: bool,
                    create_categories: bool, result: ParseResult,
                    members: dict[str, zipfile.ZipInfo] | None,
                    archive: zipfile.ZipFile | None = None) -> None:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result.file_error = (
            f"Missing header column(s): {', '.join(missing)}. "
            f"The first row must be: {','.join(REQUIRED_COLUMNS)}."
        )
        return

    # Category name → eligible Category objects (exact and casefolded lookups).
    cats = list(eligible_categories(user, official))
    exact: dict[str, list] = {}
    folded: dict[str, list] = {}
    for c in cats:
        exact.setdefault(c.name.strip(), []).append(c)
        folded.setdefault(c.name.strip().casefold(), []).append(c)

    # §F2 (Handoff #10) — BEHAVIOR CHANGE, decided: the dedupe key is now
    # (owner, question_text); category dropped out of it. Same text in two
    # categories is ONE question in both — that's the whole point of §F.
    # (Owner is the queryset filter below, so the in-memory key is the text.)
    owner_filter = {"owner__isnull": True} if official else {"owner": user}
    # §I (Handoff #9): a soft-deleted question is not a duplicate — deleting
    # one and re-uploading the same text must work.
    existing = set(
        Question.objects.filter(deleted_at__isnull=True, **owner_filter)
        .values_list("question_text", flat=True)
    )
    seen_in_file: set[str] = set()

    data_rows = list(reader)
    if len(data_rows) > MAX_ROWS:
        result.file_error = f"Too many rows — the limit is {MAX_ROWS} data rows per file ({len(data_rows)} found)."
        return

    def err(row_num, field_name, message):
        result.errors.append({"row": row_num, "field": field_name, "message": message})

    all_columns = REQUIRED_COLUMNS + MEDIA_COLUMNS
    for row_num, raw in enumerate(data_rows, start=2):
        get = lambda key: (raw.get(key) or "").strip()  # noqa: E731 — short rows read as blank
        if all(not get(col) for col in all_columns):
            continue  # silently ignore fully blank lines (trailing newlines etc.)

        # §F2 (Handoff #10): the category column accepts MULTIPLE names
        # separated by a pipe — `TV|80s` (pipe, not comma: it's a CSV). Each
        # name resolves or auto-creates exactly as one did; the row errors if
        # ANY of its names is ambiguous/ineligible (same row-error shape).
        raw_names = get("category")
        # cat_tokens: resolved Category objects and ("new", key) markers for
        # names scheduled for auto-creation, in row order, de-duped.
        cat_tokens: list = []
        cat_error = False
        names = []
        for part in raw_names.split("|"):
            part = part.strip()
            if part and part.casefold() not in {n.casefold() for n in names}:
                names.append(part)
        if not names:
            err(row_num, "category", "Category name is required.")
            cat_error = True
        for name in names:
            candidates = exact.get(name) or folded.get(name.casefold()) or []
            if not candidates:
                if create_categories:
                    # §F: schedule creation — once per distinct name (case-
                    # insensitive on the trimmed name; first casing wins).
                    new_cat_key = name.casefold()
                    result.new_categories.setdefault(new_cat_key, name)
                    cat_tokens.append(("new", new_cat_key))
                else:
                    err(row_num, "category", f'No category named "{name}" that you can add questions to.')
                    cat_error = True
            else:
                category, problem = _pick_category(name, candidates, user)
                if problem:
                    err(row_num, "category", problem)
                    cat_error = True
                else:
                    cat_tokens.append(category)

        question_text = get("question_text")
        if not question_text:
            err(row_num, "question_text", "Question text is required.")
        answer = get("answer")
        if not answer:
            err(row_num, "answer", "An answer is required.")
        elif len(answer) > MAX_ANSWER_LEN:
            err(row_num, "answer", f"Answers are limited to {MAX_ANSWER_LEN} characters ({len(answer)} given).")

        difficulty = None
        raw_difficulty = get("difficulty")
        try:
            difficulty = int(raw_difficulty)
        except ValueError:
            pass
        if difficulty is None or not 1 <= difficulty <= 5:
            err(row_num, "difficulty", f'Difficulty must be a whole number 1–5 (got "{raw_difficulty}").')

        raw_visibility = get("visibility").lower()
        visibility = raw_visibility or Visibility.PRIVATE  # blank → private
        if visibility not in (Visibility.PRIVATE, Visibility.PUBLIC):
            err(row_num, "visibility", f'Visibility must be "private", "public", or blank (got "{raw_visibility}").')

        # §G: optional media columns. Blank/none media keeps v1 behavior.
        raw_media_type = get("media_type").lower()
        media_type = raw_media_type or MediaType.NONE
        media_path = get("media_file")
        if media_type not in MediaType.values:
            err(row_num, "media_type",
                f'media_type must be "image", "audio", "video", "none", or blank (got "{raw_media_type}").')
            media_type = MediaType.NONE
        elif media_type == MediaType.NONE:
            if media_path:
                err(row_num, "media_file", 'media_file must be blank when media_type is empty or "none".')
        elif members is None:
            err(row_num, "media_type",
                "Media questions need the zip format — upload a .zip with the CSV and the media files inside.")
        elif not media_path:
            err(row_num, "media_file", f'media_file is required when media_type is "{media_type}".')
        else:
            norm = media_path.replace("\\", "/")
            while norm.startswith("./"):
                norm = norm[2:]
            info = members.get(norm)
            if info is None:
                err(row_num, "media_file", f'"{media_path}" is not in the archive.')
            else:
                _, validator = MEDIA_FIELDS[media_type]
                try:
                    validator(_ArchiveMember(info))
                    if media_type == MediaType.IMAGE and archive is not None:
                        # §F2 decompression-bomb guard, SAME check as the
                        # direct path: read only the member's image header
                        # (PIL lazy open on the zip stream) — a tiny member
                        # can otherwise decode to gigabytes.
                        with archive.open(norm) as stream:
                            check_image_pixels(stream, label="Image")
                except ValidationError as exc:
                    err(row_num, "media_file", " ".join(exc.messages))
                else:
                    media_path = norm

        if cat_error or not cat_tokens or not question_text:
            continue  # can't duplicate-check / create without valid categories + text

        # §F2: the dedupe key is the text alone (owner is the queryset scope
        # above) — same text in a second category is a duplicate NOW.
        if question_text in existing or question_text in seen_in_file:
            if skip_duplicates:
                result.skipped.append(row_num)
                continue
            # skip_duplicates=false: duplicates are created, not errored (§G2).
        seen_in_file.add(question_text)

        if media_type != MediaType.NONE:
            result.media_files += 1
            if members is not None and (info := members.get(media_path)) is not None:
                result.media_bytes_total += info.file_size  # §F3 batch storage check input
        result.rows.append(
            {
                "categories": cat_tokens,  # Category objects + ("new", key) markers
                "question_text": question_text,
                "answer": answer,
                "difficulty": difficulty,
                "visibility": visibility,
                "media_type": media_type,
                "media_file": media_path if media_type != MediaType.NONE else "",
            }
        )


def create_new_categories(new_categories: dict[str, str], user, *, official: bool) -> dict[str, "Category"]:
    """Create the §F auto-created categories (call inside the transaction).

    Non-official → owned, private, not_submitted (the creator can submit them
    for review later; half-formed name-only categories don't go straight into
    the moderation queue). Official → owner-less, public, approved (seed_demo
    shape). Returns casefolded key -> Category for `create_rows`.
    """
    mapping = {}
    for key, stored_name in new_categories.items():
        if official:
            mapping[key] = Category.objects.create(
                owner=None, name=stored_name,
                visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
            )
        else:
            mapping[key] = Category.objects.create(
                owner=user, name=stored_name,
                visibility=Visibility.PRIVATE, moderation_status=ModerationStatus.NOT_SUBMITTED,
            )
    return mapping


def create_rows(rows: list[dict], user, *, official: bool,
                category_map: dict | None = None, archive: zipfile.ZipFile | None = None) -> int:
    """Create validated rows (call inside a transaction). Returns count created.

    Media bytes are read from the archive and saved only here — after every
    row validated — so a row error can never leave orphaned files in storage.
    """
    category_map = category_map or {}
    for row in rows:
        # §F2: resolve the row's category tokens — real Category objects pass
        # through; ("new", key) markers resolve via the just-created map.
        categories = [
            token if not isinstance(token, tuple) else category_map[token[1]]
            for token in row["categories"]
        ]
        if official:
            owner, visibility, mod_status = None, Visibility.PUBLIC, ModerationStatus.APPROVED
        else:
            owner = user
            visibility = row["visibility"]
            mod_status = (
                ModerationStatus.PENDING if visibility == Visibility.PUBLIC else ModerationStatus.NOT_SUBMITTED
            )
        media_kwargs = {}
        if row["media_type"] != MediaType.NONE and archive is not None:
            field_name, _ = MEDIA_FIELDS[row["media_type"]]
            data = archive.read(row["media_file"])
            filename = posixpath.basename(row["media_file"]) or "media"
            media_kwargs[field_name] = ContentFile(data, name=filename)
        question = Question.objects.create(
            owner=owner,
            question_text=row["question_text"],
            answer=row["answer"],
            difficulty=row["difficulty"],
            visibility=visibility,
            moderation_status=mod_status,
            media_type=row["media_type"],
            **media_kwargs,
        )
        question.categories.set(categories)
    return len(rows)
