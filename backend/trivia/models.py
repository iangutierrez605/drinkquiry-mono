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
    # §F5 (Handoff #10): soft delete — same convention as Question (§I1 #9):
    # `deleted_at` is the ONLY liveness flag (null = active; no boolean twin).
    # Needed anyway once questions↔categories went M2M (deleting a category
    # no longer CASCADEs questions — M2M rows just vanish), and it kills the
    # same latent bug §I(#9) killed for questions: BoardColumn.category is
    # PROTECT, so hard-deleting an ever-PLAYED category was a guaranteed
    # ProtectedError 500. Deleted categories deliberately REMAIN in board
    # columns, snapshots, reports and history; every "active" surface
    # (listings, bulk resolution, game builds, moderation queue + counts,
    # quota counters, theme listings) filters deleted_at__isnull=True.
    # Restore/staff-delete-endpoint/revise are punted (§M).
    deleted_at = models.DateTimeField(null=True, blank=True)
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
            # §F5: PARTIAL unique (active rows only) so a deleted category's
            # name can be reused. SQLite and Postgres both support this via
            # Django's condition= (verified in both — the suite runs SQLite,
            # the owner-run compose pass runs Postgres).
            models.UniqueConstraint(
                fields=["owner", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_category_name_per_owner",
            ),
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
        """Questions a given user may put on a board from this category.
        §I1: soft-deleted questions are never usable."""
        qs = self.questions.filter(deleted_at__isnull=True)
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
    # §F1 (Handoff #10): a question can live in MORE THAN ONE category
    # ("Who shot J.R.?" is both TV and 80s) — the old `category` FK became
    # this M2M. Keeping related_name="questions" is deliberate: every
    # `category.questions` reverse lookup (usable_questions,
    # usable_question_count, similarity) keeps working with only its FILTER
    # semantics reviewed, not its spelling. Consequences stated plainly:
    #   - deleting a Category no longer CASCADEs questions (M2M rows just
    #     vanish) — §F5's category soft delete owns deletion semantics now,
    #     and NOTHING may rely on the old cascade;
    #   - a question whose categories are ALL deleted becomes undrawable
    #     (draws go through a live category) but stays in its owner's list,
    #     editable/recategorizable via PATCH;
    #   - the bulk-upload dedupe key is (owner, question_text) — category
    #     dropped out of it (same text in two categories is ONE question in
    #     both; that's the whole point of §F).
    categories = models.ManyToManyField(Category, related_name="questions")
    # §I1 (Handoff #9): soft delete. `deleted_at` is the ONLY liveness flag
    # (null = active; no boolean twin). Every "active questions" surface
    # filters deleted_at__isnull=True; deleted rows deliberately REMAIN in
    # board cells, snapshots, finished reports and usage_count aggregates
    # (history is history). Deleting always works now — BoardCell.question is
    # on_delete=PROTECT, so a hard delete of any ever-played question was a
    # guaranteed ProtectedError 500.
    deleted_at = models.DateTimeField(null=True, blank=True)
    # §I3: lineage pointer set when a staff revision supersedes this row —
    # the old row is soft-deleted and points at its replacement. SET_NULL so
    # (someday) hard-purging a replacement never cascades into history.
    replaced_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces"
    )
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


class Theme(models.Model):
    """§G (Handoff #10): a staff-curated tag grouping categories, so a host
    can find "all music categories" in one tap on the create screen.

    THIS is the tag table the owner suspected: a category can carry many
    themes; a theme groups many categories. Staff-managed, official-only for
    v1 — no owner column; creator-authored themes and theme moderation are
    §M. Themes are a discovery/selection layer only: game creation still
    takes `category_ids` and knows nothing about themes (deliberate — see
    games/services.create_game's docstring).

    Soft delete from day one (`deleted_at`, null = active — the house
    convention; three lines now vs. a migration later). The partial unique
    constraint lets a deleted theme's name be reused, exactly like §F5's
    category constraint.
    """

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    categories = models.ManyToManyField(Category, related_name="themes", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("name", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_active_theme_name",
            ),
        ]

    def __str__(self):
        return self.name


class ReportStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class QuestionReport(models.Model):
    """§K2 (Handoff #8): a host flags a public question as "not good".

    Filing a report never touches the question's moderation_status — the
    question stays listed and playable until a moderator acts (that's the
    feature's explicit ask). Moderators work reports from the /moderate
    "Flagged" tab: dismiss (keep the question, resolve the reports) or reject
    it with a note via the same field semantics the pending-queue reject uses
    (moderation_status=rejected + moderation_note), which unlists it from
    public categories and future game builds; games already built keep their
    copy (cells hold a PROTECT FK and snapshots serialize the text
    regardless of status).

    The partial unique constraint (one OPEN report per question per reporter)
    is the trivial rate limiter: re-flagging while a report is open is a 409;
    once resolved, a host may flag again if the problem persists.
    """

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="question_reports"
    )
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=ReportStatus.choices, default=ReportStatus.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["question", "reporter"],
                condition=models.Q(status="open"),
                name="one_open_report_per_question_reporter",
            ),
        ]

    def __str__(self):
        return f"Report on Q{self.question_id} by {self.reporter_id} ({self.status})"
