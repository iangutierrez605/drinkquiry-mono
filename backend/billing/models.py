"""§F2 (Handoff #18): billing models — Stripe Checkout billing with
per-purchase entitlements.

Design rulings (validated against the architecture; see the handoff):
- ONE Entitlement model, not a Game/Tournament pair: quota resolution reads
  one table, venue_tournament later is a row not a schema, reactivation
  extends `active_until` on the same row.
- NO game/tournament FKs on Purchase and NO `expired` status on it: a
  purchase is a historical payment fact; the Entitlement owns linkage and
  time owns expiry.
- Money is INTEGER MINOR UNITS (cents) — never floats; only the UI formats.
- StripeEvent carries status/error/payload beyond the brief's sketch: its
  own admin requirements ("view failed webhook events", "retry safely")
  demand them. Retry = re-run the processor on the stored payload.
- Subscription.last_event_created is the out-of-order defense: a
  subscription-state webhook applies only if its Stripe `created` >= the
  stored value (the sandbox cannot fetch live truth to reconcile).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class BillingAccount(models.Model):
    """One row per user who has ever reached Stripe — kept separate from
    User on purpose so the accounts app stays quiet and billing stays
    self-contained."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="billing_account"
    )
    stripe_customer_id = models.CharField(max_length=255, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"BillingAccount {self.user_id} ({self.stripe_customer_id})"


class StripeEventStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class StripeEvent(models.Model):
    """Webhook dedupe backbone + replayable processing record.

    `stripe_event_id` UNIQUE is the idempotency guard: the webhook view's
    FIRST act is get_or_create on it; already-seen → 200 immediately.
    `payload` stores the verified event JSON so a failed event can be
    retried by re-running the processor on the stored payload (§F9 staff
    tools) without asking Stripe for anything.
    """

    stripe_event_id = models.CharField(max_length=255, unique=True, db_index=True)
    event_type = models.CharField(max_length=100)
    status = models.CharField(
        max_length=12, choices=StripeEventStatus.choices, default=StripeEventStatus.RECEIVED
    )
    error = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-received_at", "-id")

    def __str__(self):
        return f"{self.stripe_event_id} ({self.event_type}, {self.status})"


class PurchaseStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"
    DISPUTED = "disputed", "Disputed"


class Purchase(models.Model):
    """One Checkout attempt for a one-time product. Two simultaneous
    checkouts = two pending rows, harmless; only paid ones fulfill (the
    one-entitlement-per-purchase constraint on Entitlement is the DB
    duplicate-fulfillment guard)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="purchases"
    )
    product_key = models.CharField(max_length=64)
    status = models.CharField(
        max_length=12, choices=PurchaseStatus.choices, default=PurchaseStatus.PENDING
    )
    stripe_checkout_session_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )
    stripe_payment_intent_id = models.CharField(max_length=255, null=True, blank=True)
    # Ruling: integer minor units (cents). Null until Stripe reports it.
    amount_total = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    purchased_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # The brief's "future-friendly reactivation" hook: a reactivation
    # purchase points at the entitlement it extends. Null for normal buys.
    reactivates = models.ForeignKey(
        "Entitlement", null=True, blank=True, on_delete=models.SET_NULL, related_name="reactivations"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"Purchase {self.product_key} by {self.user_id} ({self.status})"


class Subscription(models.Model):
    """Mirror of one Stripe subscription. Status strings are Stripe's own
    (active / trialing / past_due / canceled / unpaid / incomplete /
    incomplete_expired) — stored verbatim, interpreted in
    Entitlement.is_active."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stripe_subscriptions"
    )
    product_key = models.CharField(max_length=64)
    stripe_subscription_id = models.CharField(max_length=255, unique=True, db_index=True)
    stripe_price_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="active")
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True)
    # Out-of-order defense: the Stripe event `created` we last applied to
    # this row. A subscription-state webhook is applied only if its
    # `created` >= this value.
    last_event_created = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"Subscription {self.stripe_subscription_id} ({self.status})"


class EntitlementKind(models.TextChoices):
    PARTY_PACK = "party_pack", "Party pack"
    BIG_PACK = "big_pack", "Big pack"
    VENUE = "venue", "Venue"
    TOURNAMENT_PASS = "tournament_pass", "Tournament pass"
    VENUE_TOURNAMENT = "venue_tournament", "Venue tournament"


# Kinds whose content lives in BOUND categories with a per-pack question
# budget (§F4 mechanics). venue kinds are account-scoped instead.
BOUND_KINDS = (
    EntitlementKind.PARTY_PACK,
    EntitlementKind.BIG_PACK,
    EntitlementKind.TOURNAMENT_PASS,
)
VENUE_KINDS = (EntitlementKind.VENUE, EntitlementKind.VENUE_TOURNAMENT)

# Subscription statuses that mean "the lane is on" before grace enters it.
ACTIVE_SUB_STATUSES = ("active", "trialing")


class Entitlement(models.Model):
    """What a payment bought: a window of capability.

    Exactly ONE source (purchase XOR subscription) — CheckConstraint below.
    `is_active` is DERIVED (the effective_plan precedent), never stored:
      - subscription-sourced: source status in {active, trialing}, or
        past_due within its grace window; PLUS the row's own window if set
        (null active_until = follows the subscription, the normal case).
      - purchase-sourced: now within [active_from, active_until)
        (null active_until = no expiry — not used by the catalog, but the
        derived check handles it).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="entitlements"
    )
    kind = models.CharField(max_length=20, choices=EntitlementKind.choices)
    source_purchase = models.ForeignKey(
        Purchase, null=True, blank=True, on_delete=models.PROTECT, related_name="entitlements"
    )
    source_subscription = models.ForeignKey(
        Subscription, null=True, blank=True, on_delete=models.PROTECT, related_name="entitlements"
    )
    question_limit = models.PositiveIntegerField(null=True, blank=True)
    game_limit = models.PositiveIntegerField(null=True, blank=True)
    # Set when a tournament_pass is consumed — the "one pass, one
    # tournament" rule as a DB constraint (OneToOne).
    tournament = models.OneToOneField(
        "games.Tournament", null=True, blank=True, on_delete=models.SET_NULL, related_name="entitlement"
    )
    active_from = models.DateTimeField(default=timezone.now)
    active_until = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            # Exactly one source.
            models.CheckConstraint(
                condition=(
                    models.Q(source_purchase__isnull=False, source_subscription__isnull=True)
                    | models.Q(source_purchase__isnull=True, source_subscription__isnull=False)
                ),
                name="entitlement_exactly_one_source",
            ),
            # One entitlement per purchase — the duplicate-fulfillment DB
            # guard the brief asks for (a replayed webhook can't grant twice).
            models.UniqueConstraint(
                fields=["source_purchase"],
                condition=models.Q(source_purchase__isnull=False),
                name="one_entitlement_per_purchase",
            ),
        ]

    @property
    def is_active(self) -> bool:
        now = timezone.now()
        # The row's own window binds in all cases when set (reactivation
        # writes active_until; subscription rows normally leave it null).
        if self.active_from and now < self.active_from:
            return False
        if self.active_until is not None and now >= self.active_until:
            return False
        if self.source_subscription_id:
            sub = self.source_subscription
            if sub.status in ACTIVE_SUB_STATUSES:
                return True
            if (
                sub.status == "past_due"
                and sub.grace_period_ends_at
                and now < sub.grace_period_ends_at
            ):
                return True
            return False
        return True  # purchase-sourced, inside its window

    def __str__(self):
        return f"Entitlement {self.kind} for {self.user_id}"


class BillingAuditLog(models.Model):
    """§F9 groundwork (schema ships now, staff tools later): every manual
    staff billing change writes one row with a REQUIRED reason."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="billing_audit_entries"
    )
    target_entitlement = models.ForeignKey(
        Entitlement, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    field = models.CharField(max_length=64)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
