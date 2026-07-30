from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


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

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

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
