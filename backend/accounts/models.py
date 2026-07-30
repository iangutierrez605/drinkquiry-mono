from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# §H (Handoff #11): the brand logo rides the SAME validation + resize
# pipeline as every other image (trivia/validators.py + trivia/images.py).
# Module-level import is safe: trivia.validators only touches settings and
# ValidationError at import time (the PIL import inside it is deferred).
from trivia.validators import validate_image


class Plan(models.TextChoices):
    FREE = "free", "Free"
    CREATOR = "creator", "Creator"


class UserManager(BaseUserManager):
    """Email is the unique identifier; no username."""

    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(_("email address"), unique=True)
    display_name = models.CharField(max_length=50, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    date_joined = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # --- Monetisation ---------------------------------------------------
    # `plan` is the single source of truth for entitlements. Today an admin
    # sets it by hand; a Stripe webhook will write the same two fields later.
    # Quotas per plan live in settings.PLAN_LIMITS (no per-user rows).
    plan = models.CharField(
        max_length=20,
        choices=Plan.choices,
        default=Plan.FREE,
        help_text="Paid tier. Quotas come from settings.PLAN_LIMITS.",
    )
    plan_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the paid plan lapses (blank = never). Past dates behave as free.",
    )
    # §J1 (Handoff #9): per-user quota overrides — same keys as a PLAN_LIMITS
    # entry (games_per_month, categories, questions, storage_bytes), all
    # optional. An override key REPLACES the plan's value for that key; null
    # means unlimited (PLAN_LIMITS convention); a missing key falls through to
    # the plan. Merged in exactly ONE place: accounts/quotas.limits_for().
    # Note the deliberate semantics (pinned by tests): an override is a grant
    # to the USER, not to the plan — it still applies after a paid plan lapses
    # to free.
    limit_overrides = models.JSONField(default=dict, blank=True)

    # --- §H (Handoff #11): venue branding -------------------------------
    # The brand lives on the USER, not the game: every game this user hosts
    # is branded (that's what a venue wants — one place to set, not per-game
    # fiddling; per-game overrides are §M). Serving is gated on the
    # EFFECTIVE plan (see games' GameStateSerializer.get_brand): a lapsed
    # creator plan turns branding OFF without destroying the venue's upload.
    # Writes are gated in ProfileView (free-plan write → plain 403); the
    # staff Users tab may edit brand_name and clear the logo.
    brand_name = models.CharField(max_length=60, blank=True)
    brand_logo = models.ImageField(
        upload_to="brands/", null=True, blank=True, validators=[validate_image]
    )
    # §H1: persisted size of `brand_logo` (post-resize), maintained by save()
    # below exactly like Category.photo_bytes — the storage quota counts it
    # (accounts/quotas.storage_bytes_used). Clearing the logo frees the bytes
    # because this column goes to 0 with it.
    brand_logo_bytes = models.PositiveBigIntegerField(default=0)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        # §H1: the models' save()-time media choke point, exactly like
        # Category.save() — resize BEFORE storage, recount brand_logo_bytes
        # from the post-resize file (or to 0 when the logo is cleared by a
        # full save).
        from trivia.images import prepare_media

        prepare_media(
            self,
            image_fields=("brand_logo",),
            file_fields=("brand_logo",),
            byte_field="brand_logo_bytes",
            update_fields=kwargs.get("update_fields"),
        )
        super().save(*args, **kwargs)

    @property
    def effective_plan(self) -> str:
        """The plan that actually applies right now (expiry collapses to free)."""
        if self.plan != Plan.FREE and self.plan_expires_at and self.plan_expires_at <= timezone.now():
            return Plan.FREE
        return self.plan

    @property
    def is_creator(self) -> bool:
        """Kept for existing readers (IsCreator permission, profile serializer).

        Derived, no longer a column: true iff the effective plan is paid.
        """
        return self.effective_plan != Plan.FREE

    def __str__(self):
        return self.email
