"""Image processing + media byte accounting (Handoff #7 §F2/§F3).

ONE shared implementation for BOTH upload entrances:

- the direct create/PATCH path (QuestionSerializer / CategorySerializer →
  model .save()), and
- the bulk zip path (`create_rows` → Question.objects.create → .save()).

Both funnel through the models' save() overrides, which call
`process_media_fields()` below. The resize therefore happens BEFORE the file
hits storage — never store-then-resize (in Spaces mode that would be an extra
round-trip and a window where the oversized original exists in the bucket).

Policy (numbers in settings):
- auto-resize when size > IMAGE_RESIZE_THRESHOLD_BYTES (1 MB) OR longest edge
  > IMAGE_MAX_DIMENSION (1920 px);
- EXIF orientation applied FIRST (ImageOps.exif_transpose), then downscale to
  ≤ 1920 px longest edge;
- re-encode: JPEG quality ~82 for photographic input; PNG (optimized) is
  preserved when the source carries an alpha channel;
- EXIF is stripped on the way out (privacy: phone photos carry GPS) — a plain
  re-encode without an `exif=` kwarg drops it;
- animated GIFs are left byte-for-byte intact (a resize would flatten the
  animation); they remain subject to the hard byte/pixel rejects;
- files PIL cannot parse are left untouched: the direct path already rejects
  them (DRF's ImageField verifies with Pillow) and the bulk path's contract
  (pinned by tests) validates zip media by size + extension type only.

The decompression-bomb guard (`check_image_pixels`) reads only the image
HEADER (PIL's lazy open) and rejects > MAX_IMAGE_PIXELS before any full
decode — a tiny zip member can otherwise decode to gigabytes of RAM.
`Image.MAX_IMAGE_PIXELS` is set as a backstop for any accidental decode
elsewhere.
"""
import io
import posixpath
import warnings

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps

# Backstop only — the explicit header check below is the enforced limit.
# Set at 2× so files between 1× and 2× the cap are rejected by OUR check with
# a clear "megapixels" message instead of first tripping Pillow's
# DecompressionBombWarning inside Django/DRF's image verification (which
# would make the reject message nondeterministic and the suite noisy).
# Anything past ~2× the cap still hard-errors inside Pillow itself, which the
# callers below turn into the same clean ValidationError.
Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS * 2

# Modes that carry (or can carry) transparency worth preserving as PNG.
_ALPHA_MODES = {"RGBA", "LA", "PA"}


def _open_header(fileobj):
    """Lazily open an image (header only) and rewind; None if unparseable.

    Raises Image.DecompressionBombError through: Pillow refuses to even
    header-open files past 2× MAX_IMAGE_PIXELS, and swallowing that would let
    the very worst bombs skip the pixel check. The 1× warning is suppressed —
    check_image_pixels turns those into a clean ValidationError instead.
    """
    try:
        fileobj.seek(0)
    except (AttributeError, OSError):
        pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(fileobj)
            img.size  # forces the header parse, not the pixel decode
        return img
    except Image.DecompressionBombError:
        raise
    except Exception:  # noqa: BLE001 — not an image PIL understands
        try:
            fileobj.seek(0)
        except (AttributeError, OSError):
            pass
        return None


def check_image_pixels(fileobj, label="Image"):
    """§F2 decompression-bomb guard: reject > MAX_IMAGE_PIXELS from the
    header alone, BEFORE any full decode. Non-parseable files pass through —
    other layers own that decision (see module docstring)."""
    try:
        img = _open_header(fileobj)
    except Image.DecompressionBombError:
        raise ValidationError(
            f"{label} is far too large to decode safely. "
            f"Max is {settings.MAX_IMAGE_PIXELS // 1_000_000} megapixels."
        )
    if img is None:
        return
    width, height = img.size
    pixels = width * height
    try:
        fileobj.seek(0)
    except (AttributeError, OSError):
        pass
    if pixels > settings.MAX_IMAGE_PIXELS:
        raise ValidationError(
            f"{label} is too large ({width}×{height} ≈ {pixels // 1_000_000} megapixels). "
            f"Max is {settings.MAX_IMAGE_PIXELS // 1_000_000} megapixels."
        )


def _has_alpha(img) -> bool:
    return img.mode in _ALPHA_MODES or (img.mode == "P" and "transparency" in img.info)


def maybe_resize_image(fieldfile):
    """Return a replacement ContentFile for an oversized image, or None.

    `fieldfile` is an UNCOMMITTED FieldFile (fresh upload). Never upscales;
    output is ≤ IMAGE_MAX_DIMENSION on the longest edge, EXIF-oriented,
    EXIF-stripped, JPEG q~82 (or optimized PNG when alpha is present).
    """
    size = fieldfile.size
    raw = fieldfile.file
    try:
        img = _open_header(raw)
    except Image.DecompressionBombError:
        return None  # rejection is the validators' job; never store-and-crash here
    if img is None:
        return None  # not PIL-parseable — leave it alone (see module docstring)
    if getattr(img, "is_animated", False):
        return None  # animated GIF/WebP: a resize would flatten it
    longest = max(img.size)
    if size <= settings.IMAGE_RESIZE_THRESHOLD_BYTES and longest <= settings.IMAGE_MAX_DIMENSION:
        return None

    # EXIF orientation FIRST (a rotated phone photo must not be resized on
    # its side), then downscale. thumbnail() never upscales.
    img = ImageOps.exif_transpose(img)
    limit = settings.IMAGE_MAX_DIMENSION
    img.thumbnail((limit, limit), Image.Resampling.LANCZOS)

    stem = posixpath.splitext(posixpath.basename(fieldfile.name or "image"))[0] or "image"
    buf = io.BytesIO()
    if _has_alpha(img):
        if img.mode not in ("RGBA", "LA"):
            img = img.convert("RGBA")
        img.save(buf, format="PNG", optimize=True)  # no exif kwarg → EXIF stripped
        new_name = f"{stem}.png"
    else:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=settings.IMAGE_JPEG_QUALITY, optimize=True)
        new_name = f"{stem}.jpg"
    return ContentFile(buf.getvalue(), name=new_name)


def prepare_media(instance, *, image_fields, file_fields, byte_field, update_fields=None):
    """The models' save()-time choke point (§F2 resize + §F3 accounting).

    - Resizes any UNCOMMITTED image in `image_fields` in place (so the resized
      bytes are what pre_save hands to storage).
    - Recomputes `byte_field` (media_bytes / photo_bytes) as the sum of the
      sizes of every set file in `file_fields` — post-resize by construction,
      since the resize just ran.

    Recount triggers: a new row, any fresh (uncommitted) file, or a full save
    (update_fields=None) — the last one keeps the count honest when a PATCH
    clears a file. Targeted saves like the moderation queue's
    `save(update_fields=[...])` skip the recount entirely, so approving
    content never costs a storage round-trip per committed file.
    """
    uncommitted = [
        name for name in file_fields if (f := getattr(instance, name)) and not f._committed
    ]
    for name in uncommitted:
        if name in image_fields:
            replacement = maybe_resize_image(getattr(instance, name))
            if replacement is not None:
                setattr(instance, name, replacement)
    if instance.pk is None or uncommitted or update_fields is None:
        setattr(instance, byte_field, media_bytes_total(instance, file_fields))


def media_bytes_total(instance, file_fields) -> int:
    """Sum of the set files' sizes. Committed files whose backing object is
    missing (partial backfills, hand-edited rows) count as 0 — best effort,
    never a crash."""
    total = 0
    for name in file_fields:
        f = getattr(instance, name)
        if not f:
            continue
        try:
            total += f.size
        except (OSError, ValueError):
            pass
    return total
