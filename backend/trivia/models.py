from django.conf import settings
from django.db import models

from .validators import validate_audio, validate_image, validate_video


class Visibility(models.TextChoices):
    PRIVATE = "private", "Private"
    PUBLIC = "public", "Public"


class ModerationStatus(models.TextChoices):
    # Private content never enters moderation.
    NOT_SUBMITTED = "not_submitted", "Not submitted"
    PENDING = "pending", "Pending review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class ModeratedContentMixin(models.Model):
    """Shared owner/visibility/vetting fields for categories and questions."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        help_text="Null for official (site-provided) content.",
    )
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE)
    moderation_status = models.CharField(
        max_length=20, choices=ModerationStatus.choices, default=ModerationStatus.NOT_SUBMITTED
    )
    moderation_note = models.TextField(blank=True, help_text="Reviewer feedback on rejection.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def is_publicly_visible(self) -> bool:
        return (
            self.visibility == Visibility.PUBLIC
            and self.moderation_status == ModerationStatus.APPROVED
        )

    def submit_for_review(self):
        self.visibility = Visibility.PUBLIC
        self.moderation_status = ModerationStatus.PENDING


class Category(ModeratedContentMixin):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to="categories/", null=True, blank=True, validators=[validate_image])
    # §F3: persisted size of `photo` (post-resize), maintained by save() below
    # so the storage quota is counted from columns, never by listing a bucket.
    photo_bytes = models.PositiveBigIntegerField(default=0)

    def save(self, *args, **kwargs):
        # §F2/§F3 choke point — BOTH upload entrances (serializer and bulk)
        # end in save(), so the resize runs before the file hits storage and
        # photo_bytes is recomputed from the post-resize file.
        from .images import prepare_media

        prepare_media(
            self,
            image_fields=("photo",),
            file_fields=("photo",),
            byte_field="photo_bytes",
            update_fields=kwargs.get("update_fields"),
        )
        super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_category_name_per_owner"),
        ]

    def __str__(self):
        return self.name

    def accepts_questions_from(self, user) -> bool:
        """May `user` add questions to this category?

        The single source of the rule (official | own | publicly-visible) —
        enforced by QuestionSerializer.validate and the bulk CSV importer.
        """
        return self.owner_id in (None, user.id) or self.is_publicly_visible

    def usable_question_count(self, user=None) -> int:
        """Questions a given user may put on a board from this category."""
        qs = self.questions.all()
        public = models.Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)
        if user is not None and user.is_authenticated:
            return qs.filter(public | models.Q(owner=user)).count()
        return qs.filter(public).count()


class MediaType(models.TextChoices):
    NONE = "none", "None"
    IMAGE = "image", "Image"
    AUDIO = "audio", "Audio"
    VIDEO = "video", "Video"


class Question(ModeratedContentMixin):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="questions")
    question_text = models.TextField()
    answer = models.CharField(max_length=500)
    difficulty = models.PositiveSmallIntegerField(
        default=1, help_text="1 (easy) to 5 (hard); drives board row/value placement."
    )
    media_type = models.CharField(max_length=10, choices=MediaType.choices, default=MediaType.NONE)
    image = models.ImageField(upload_to="questions/images/", null=True, blank=True, validators=[validate_image])
    audio = models.FileField(upload_to="questions/audio/", null=True, blank=True, validators=[validate_audio])
    video = models.FileField(upload_to="questions/video/", null=True, blank=True, validators=[validate_video])
    # §F3: persisted sum of the image/audio/video sizes (post-resize),
    # maintained by save() below. The per-owner storage quota in
    # accounts/quotas.py is a Sum() over this column — never a bucket listing.
    media_bytes = models.PositiveBigIntegerField(default=0)

    def save(self, *args, **kwargs):
        # §F2/§F3 choke point — see Category.save. Direct create/PATCH and the
        # bulk zip path both land here, which is what keeps their behavior
        # identical (one shared implementation, tested from both entrances).
        from .images import prepare_media

        prepare_media(
            self,
            image_fields=("image",),
            file_fields=("image", "audio", "video"),
            byte_field="media_bytes",
            update_fields=kwargs.get("update_fields"),
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.question_text[:80]
