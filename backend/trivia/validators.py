from django.conf import settings
from django.core.exceptions import ValidationError

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
AUDIO_TYPES = {"audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}


def _validate(file, max_bytes, allowed_types, label):
    if file.size > max_bytes:
        raise ValidationError(
            f"{label} too large ({file.size // (1024 * 1024)} MB). "
            f"Max is {max_bytes // (1024 * 1024)} MB."
        )
    content_type = getattr(file, "content_type", None)
    if content_type and content_type not in allowed_types:
        raise ValidationError(f"Unsupported {label.lower()} type: {content_type}.")


def validate_image(file):
    _validate(file, settings.MAX_IMAGE_BYTES, IMAGE_TYPES, "Image")
    # §F2 decompression-bomb guard: reject absurd pixel counts from the image
    # HEADER, before any full decode. Only possible when the validator holds
    # real bytes (the direct upload path); the bulk zip path vets members
    # pre-extraction through a size/type stand-in and runs this same check on
    # the member's stream in bulk_upload.py.
    if hasattr(file, "read"):
        from .images import check_image_pixels  # local import: avoid PIL at migration import time

        check_image_pixels(file, label="Image")


def validate_audio(file):
    _validate(file, settings.MAX_AUDIO_BYTES, AUDIO_TYPES, "Audio")


def validate_video(file):
    _validate(file, settings.MAX_VIDEO_BYTES, VIDEO_TYPES, "Video")
