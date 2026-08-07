"""§F3 (Handoff #18): webhook event processing — the money loop's server
half. The VIEW verifies the signature and owns StripeEvent dedupe; each
handler here runs inside transaction.atomic with a row lock on the Purchase
or Subscription it mutates, and every handler is IDEMPOTENT (replaying any
event changes nothing — pinned by tests).

Field-location note (stripe 15.4.0 / API 2026-07-29.dahlia, verified at
build time — see catalog.configure_stripe): billing-period stamps live on
the SUBSCRIPTION ITEM (subscription["items"]["data"][0]) and an invoice's
subscription id is read from the top-level "subscription" falling back to
parent.subscription_details.subscription.
"""
import datetime
import logging

from django.db import transaction
from django.utils import timezone

from .catalog import get_product
from .emails import (
    send_payment_failed,
    send_purchase_confirmation,
    send_renewal_receipt,
    send_subscription_started,
)
from .models import (
    Entitlement,
    Purchase,
    PurchaseStatus,
    Subscription,
)

logger = logging.getLogger(__name__)


class ProcessingError(Exception):
    """Marks the StripeEvent failed and returns 500 so Stripe retries."""


def _ts(value) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    return datetime.datetime.fromtimestamp(int(value), tz=datetime.timezone.utc)


def _locked_purchase(session_id: str) -> Purchase | None:
    # House rule (#14b CHANGES): locked querysets carry NO select_related —
    # FOR UPDATE across an outer join on a nullable FK (reactivates) is the
    # known Postgres foot-gun. Related rows load lazily after the lock.
    return (
        Purchase.objects.select_for_update()
        .filter(stripe_checkout_session_id=session_id)
        .first()
    )


def process_event(event: dict) -> str:
    """Dispatch one VERIFIED event dict. Returns processed/skipped; raises
    ProcessingError on failures that should make Stripe retry."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    created = int(event.get("created") or 0)
    handler = _HANDLERS.get(etype)
    if handler is None:
        return "skipped"  # not an event we subscribe to; 200 and move on
    with transaction.atomic():
        return handler(obj, created) or "processed"


# --- checkout ----------------------------------------------------------------


def _recover_purchase_from_metadata(session: dict) -> Purchase | None:
    """§F4 (Handoff #19): rebuild a LOST pending Purchase from the session's
    own metadata. Checkout stamps every session with drinkquiry_user_id +
    product_key (+ reactivates for reactivation buys), and metadata arrives
    inside the SIGNATURE-VERIFIED payload — it is trustworthy. A row can be
    missing when the process died between Stripe accepting the session and
    our DB commit; without recovery that sale could never fulfill. Foreign
    sessions (Dashboard payment links, other products on the account) carry
    none of our metadata and return None — the callers then SKIP with a 200
    instead of 500ing into a forever-retry (C-4).

    Race-safe: get_or_create on the unique session id (a concurrent event's
    worker may have just rebuilt it), then returned RE-LOCKED via
    _locked_purchase (select_for_update; house no-select_related rule)."""
    metadata = session.get("metadata") or {}
    session_id = str(session.get("id") or "")
    user_id = str(metadata.get("drinkquiry_user_id") or "")
    product_key = str(metadata.get("product_key") or "")
    if not (session_id and user_id and product_key):
        return None
    entry = get_product(product_key)
    if entry is None:
        return None
    from accounts.models import User  # lazy — the accounts↔billing convention

    try:
        user = User.objects.filter(pk=int(user_id)).first()
    except (TypeError, ValueError):
        user = None
    if user is None:
        return None
    reactivates = None
    if entry.get("reactivation_of"):
        # A reactivation rebuilt WITHOUT a valid target could never fulfill
        # (_fulfill_paid_purchase would ProcessingError forever) — treat an
        # unresolvable target as unrecoverable and let the caller skip.
        try:
            ent_id = int(metadata.get("reactivates"))
        except (TypeError, ValueError):
            ent_id = None
        if ent_id is not None:
            reactivates = Entitlement.objects.filter(
                pk=ent_id, user=user, kind=entry["reactivation_of"]
            ).first()
        if reactivates is None:
            logger.warning(
                "Session %s looks like ours (user=%s, product=%s) but its "
                "reactivation target is unresolvable — not rebuilding.",
                session_id, user.pk, product_key,
            )
            return None
    Purchase.objects.get_or_create(
        stripe_checkout_session_id=session_id,
        defaults={
            "user": user,
            "product_key": product_key,
            "reactivates": reactivates,
            "metadata": {
                "recovered_from_metadata": True,
                **({"reactivates": reactivates.pk} if reactivates else {}),
            },
        },
    )
    logger.warning(
        "Rebuilt the Purchase row for session %s from its signature-verified "
        "metadata (user=%s, product=%s) — the pending row was lost before commit.",
        session_id, user.pk, product_key,
    )
    return _locked_purchase(session_id)


def _foreign_session_skip(session_id: str, event_label: str) -> str:
    logger.warning(
        "%s for unknown session %s with no Drinkquiry metadata — skipping. "
        "Payment links don't grant anything; purchases must go through the "
        "site's checkout so they attach to an account.",
        event_label, session_id,
    )
    return "skipped"


def _fulfill_paid_purchase(purchase: Purchase, session: dict) -> None:
    """Grant exactly once. The one-entitlement-per-purchase constraint is
    the DB backstop; the status check makes replays no-ops first."""
    entry = get_product(purchase.product_key)
    if entry is None:
        raise ProcessingError(f"Unknown product key {purchase.product_key!r} on purchase {purchase.pk}")
    if purchase.status == PurchaseStatus.PAID:
        return  # replayed event — nothing to do
    purchase.status = PurchaseStatus.PAID
    purchase.stripe_payment_intent_id = str(session.get("payment_intent") or "") or None
    if session.get("amount_total") is not None:
        purchase.amount_total = int(session["amount_total"])
    purchase.currency = str(session.get("currency") or "")
    purchase.purchased_at = timezone.now()
    purchase.save(
        update_fields=[
            "status", "stripe_payment_intent_id", "amount_total", "currency", "purchased_at",
        ]
    )

    if entry.get("reactivation_of"):
        # §F4 reactivation (dark lane): extend the pointed-at entitlement by
        # window_days from max(now, old end). No new entitlement row.
        ent = purchase.reactivates
        if ent is None or ent.user_id != purchase.user_id or ent.kind != entry["reactivation_of"]:
            raise ProcessingError(f"Reactivation purchase {purchase.pk} has no valid target entitlement")
        base = max(timezone.now(), ent.active_until or timezone.now())
        ent.active_until = base + datetime.timedelta(days=entry["window_days"])
        ent.save(update_fields=["active_until"])
        send_purchase_confirmation(
            purchase.user, entry["display_name"], None,
            amount_total=purchase.amount_total, currency=purchase.currency,
            purchased_at=purchase.purchased_at,
            reference=purchase.stripe_payment_intent_id or "",
        )
        return

    entitlement = Entitlement.objects.create(
        user=purchase.user,
        kind=entry["kind"],
        source_purchase=purchase,
        question_limit=entry.get("question_limit"),
        game_limit=entry.get("game_limit"),
        active_from=timezone.now(),
        active_until=timezone.now() + datetime.timedelta(days=entry["window_days"])
        if entry.get("window_days")
        else None,
    )
    # #19.1 (owner ruling): NO auto-created starter category. The #18
    # starter solved a cold start that no longer exists — /create's
    # category form now offers the pack lane directly (and the caps grew
    # to 10/20), so buyers organize their own boards from question one.
    # The confirmation email's no-category branch points them at /create.
    send_purchase_confirmation(
        purchase.user, entry["display_name"], None,
        guide_to_create=True,
        amount_total=purchase.amount_total, currency=purchase.currency,
        purchased_at=purchase.purchased_at,
        reference=purchase.stripe_payment_intent_id or "",
    )


def _handle_checkout_completed(session: dict, created: int) -> str | None:
    mode = session.get("mode")
    session_id = str(session.get("id") or "")
    # §F4 (#19): locked row → else rebuild from signature-verified metadata
    # → else it's a FOREIGN sale (Dashboard payment link, another product on
    # the same Stripe account): skip loudly with a 200. The old
    # ProcessingError made Stripe retry a grant that can never happen,
    # forever, on every payment-link sale (C-4).
    purchase = _locked_purchase(session_id)
    if purchase is None:
        purchase = _recover_purchase_from_metadata(session)
    if purchase is None:
        return _foreign_session_skip(session_id, "checkout.session.completed")
    if mode == "payment":
        # Brief rule: grant only when actually PAID (async methods complete
        # later via async_payment_succeeded).
        if session.get("payment_status") == "paid":
            _fulfill_paid_purchase(purchase, session)
        return None
    if mode == "subscription":
        _start_subscription(session)
        return None
    return "skipped"


def _handle_async_payment_succeeded(session: dict, created: int) -> str | None:
    session_id = str(session.get("id") or "")
    purchase = _locked_purchase(session_id)
    if purchase is None:
        purchase = _recover_purchase_from_metadata(session)  # §F4 (#19)
    if purchase is None:
        return _foreign_session_skip(session_id, "checkout.session.async_payment_succeeded")
    _fulfill_paid_purchase(purchase, session)
    return None


def _handle_async_payment_failed(session: dict, created: int) -> str | None:
    purchase = _locked_purchase(str(session.get("id") or ""))
    if purchase is not None and purchase.status == PurchaseStatus.PENDING:
        purchase.status = PurchaseStatus.FAILED
        purchase.save(update_fields=["status"])
    return None


def _handle_checkout_expired(session: dict, created: int) -> str | None:
    # The brief omitted this; the handoff adds it: an abandoned session's
    # pending Purchase is marked failed so /status/ stops saying "pending".
    purchase = _locked_purchase(str(session.get("id") or ""))
    if purchase is not None and purchase.status == PurchaseStatus.PENDING:
        purchase.status = PurchaseStatus.FAILED
        purchase.save(update_fields=["status"])
    return None


def _start_subscription(session: dict) -> None:
    """checkout.session.completed, subscription mode: store ids, create the
    Subscription row + venue entitlement. invoice/subscription events are
    the ongoing truth from here."""
    purchase = _locked_purchase(str(session.get("id") or ""))
    if purchase is None:
        # §F4 (#19): unreachable in practice — _handle_checkout_completed
        # recovers or skips BEFORE calling here. Kept as a guard so a future
        # direct caller can't fulfill a session nobody owns.
        raise ProcessingError("subscription checkout completed for unknown session")
    entry = get_product(purchase.product_key)
    if entry is None or entry["mode"] != "subscription":
        raise ProcessingError(f"Session {session.get('id')} product mismatch")
    sub_id = str(session.get("subscription") or "")
    if not sub_id:
        raise ProcessingError("subscription-mode session carries no subscription id")
    if purchase.status != PurchaseStatus.PAID:
        purchase.status = PurchaseStatus.PAID
        if session.get("amount_total") is not None:
            purchase.amount_total = int(session["amount_total"])
        purchase.currency = str(session.get("currency") or "")
        purchase.purchased_at = timezone.now()
        purchase.save(update_fields=["status", "amount_total", "currency", "purchased_at"])
    subscription, created_row = Subscription.objects.select_for_update().get_or_create(
        stripe_subscription_id=sub_id,
        defaults={"user": purchase.user, "product_key": purchase.product_key, "status": "active"},
    )
    if created_row or not subscription.entitlements.exists():
        Entitlement.objects.get_or_create(
            source_subscription=subscription,
            defaults={
                "user": purchase.user,
                "kind": entry["kind"],
                "question_limit": entry.get("question_limit"),
                "game_limit": entry.get("game_limit"),
                # active_until stays NULL: activeness derives from the
                # subscription (§F2).
            },
        )
    if created_row:
        send_subscription_started(purchase.user, entry["display_name"])


# --- subscription lifecycle --------------------------------------------------


def _invoice_subscription_id(invoice: dict) -> str:
    """Top-level `subscription` first; 2026 API surfaces may nest it under
    parent.subscription_details instead (verified against the pinned SDK)."""
    sub = invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if not sub:
        sub = ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if isinstance(sub, dict):
            sub = sub.get("id")
    return str(sub or "")


def _locked_subscription(sub_id: str) -> Subscription | None:
    if not sub_id:
        return None
    # No select_related on locked querysets (house rule — see _locked_purchase).
    return (
        Subscription.objects.select_for_update()
        .filter(stripe_subscription_id=sub_id)
        .first()
    )


def _handle_invoice_paid(invoice: dict, created: int) -> str | None:
    sub = _locked_subscription(_invoice_subscription_id(invoice))
    if sub is None:
        return "skipped"  # an invoice for a subscription we never sold
    # Paid-through forward; restore from past_due; clear grace.
    sub.status = "active"
    sub.grace_period_ends_at = None
    period_end = _ts((invoice.get("lines") or {}).get("data", [{}])[0].get("period", {}).get("end"))
    if period_end:
        sub.current_period_end = period_end
    period_start = _ts((invoice.get("lines") or {}).get("data", [{}])[0].get("period", {}).get("start"))
    if period_start:
        sub.current_period_start = period_start
    sub.save(
        update_fields=["status", "grace_period_ends_at", "current_period_start", "current_period_end"]
    )
    # Owner ruling (#18 follow-up): the app is the receipt sender — every
    # successful charge gets one, renewals included. $0 invoices (trials,
    # full-credit) send nothing. Replay-safe: the view's StripeEvent dedupe
    # means a re-delivered invoice.paid never reaches this line twice.
    amount_paid = int(invoice.get("amount_paid") or 0)
    if amount_paid > 0:
        entry = get_product(sub.product_key) or {"display_name": sub.product_key}
        send_renewal_receipt(
            sub.user, entry["display_name"], amount_paid,
            str(invoice.get("currency") or ""), sub.current_period_end,
        )
    return None


def _handle_invoice_payment_failed(invoice: dict, created: int) -> str | None:
    sub = _locked_subscription(_invoice_subscription_id(invoice))
    if sub is None:
        return "skipped"
    from django.conf import settings

    already_in_grace = sub.status == "past_due" and sub.grace_period_ends_at
    sub.status = "past_due"
    if not already_in_grace:
        sub.grace_period_ends_at = timezone.now() + datetime.timedelta(days=settings.BILLING_GRACE_DAYS)
    sub.save(update_fields=["status", "grace_period_ends_at"])
    if not already_in_grace:
        entry = get_product(sub.product_key) or {"display_name": sub.product_key}
        send_payment_failed(sub.user, entry["display_name"])
    return None


def _handle_subscription_updated(sub_obj: dict, created: int) -> str | None:
    sub = _locked_subscription(str(sub_obj.get("id") or ""))
    if sub is None:
        return "skipped"
    # Out-of-order defense (§F2): only apply if this event isn't older than
    # what we've already applied.
    if created and created < sub.last_event_created:
        return "skipped"
    status = str(sub_obj.get("status") or sub.status)
    sub.status = status
    sub.cancel_at_period_end = bool(sub_obj.get("cancel_at_period_end", sub.cancel_at_period_end))
    if sub_obj.get("canceled_at"):
        sub.canceled_at = _ts(sub_obj["canceled_at"])
    # Periods: SubscriptionItem in this API version (see module docstring).
    items = ((sub_obj.get("items") or {}).get("data") or [{}])[0]
    if items.get("current_period_start"):
        sub.current_period_start = _ts(items["current_period_start"])
    if items.get("current_period_end"):
        sub.current_period_end = _ts(items["current_period_end"])
    price = (items.get("price") or {}).get("id")
    if price:
        sub.stripe_price_id = str(price)
    if status in ("active", "trialing"):
        sub.grace_period_ends_at = None
    if created:
        sub.last_event_created = created
    sub.save(
        update_fields=[
            "status", "cancel_at_period_end", "canceled_at", "current_period_start",
            "current_period_end", "stripe_price_id", "grace_period_ends_at", "last_event_created",
        ]
    )
    return None


def _handle_subscription_deleted(sub_obj: dict, created: int) -> str | None:
    sub = _locked_subscription(str(sub_obj.get("id") or ""))
    if sub is None:
        return "skipped"
    if created and created < sub.last_event_created:
        return "skipped"
    sub.status = "canceled"
    sub.canceled_at = _ts(sub_obj.get("canceled_at")) or timezone.now()
    sub.grace_period_ends_at = None
    if created:
        sub.last_event_created = created
    sub.save(update_fields=["status", "canceled_at", "grace_period_ends_at", "last_event_created"])
    # Content preserved — deletion of nothing, ever (brief rule). The
    # entitlement's is_active goes false on its own via the derived check.
    return None


# --- refunds / disputes ------------------------------------------------------


def _entitlement_substantially_used(entitlement: Entitlement) -> bool:
    """§F3 refund rule: any hosted game touching the pack's bound
    categories, or (pass) any finished game in its tournament."""
    from games.models import Game, GameStatus

    if entitlement.tournament_id is not None:
        if Game.objects.filter(
            tournament_id=entitlement.tournament_id, status=GameStatus.FINISHED
        ).exists():
            return True
    return Game.objects.filter(columns__category__entitlement=entitlement).exists()


def _mark_purchase(payment_intent_id: str, new_status: str) -> str | None:
    if not payment_intent_id:
        return "skipped"
    purchase = (
        Purchase.objects.select_for_update()
        .filter(stripe_payment_intent_id=payment_intent_id)
        .first()
    )
    if purchase is None:
        return "skipped"
    purchase.status = new_status
    purchase.save(update_fields=["status"])
    entitlement = getattr(purchase, "entitlements", None) and purchase.entitlements.first()
    if entitlement is not None:
        if _entitlement_substantially_used(entitlement):
            # FLAG for manual review instead of auto-revoking (brief rule).
            entitlement.metadata = {**(entitlement.metadata or {}), "manual_review": new_status}
            entitlement.save(update_fields=["metadata"])
            logger.warning(
                "Entitlement %s flagged for manual review after %s (substantially used)",
                entitlement.pk, new_status,
            )
        else:
            entitlement.active_until = timezone.now()
            entitlement.save(update_fields=["active_until"])
    return None


def _handle_charge_refunded(charge: dict, created: int) -> str | None:
    return _mark_purchase(str(charge.get("payment_intent") or ""), PurchaseStatus.REFUNDED)


def _handle_dispute_created(dispute: dict, created: int) -> str | None:
    return _mark_purchase(str(dispute.get("payment_intent") or ""), PurchaseStatus.DISPUTED)


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "checkout.session.async_payment_succeeded": _handle_async_payment_succeeded,
    "checkout.session.async_payment_failed": _handle_async_payment_failed,
    "checkout.session.expired": _handle_checkout_expired,
    "invoice.paid": _handle_invoice_paid,
    "invoice.payment_failed": _handle_invoice_payment_failed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "charge.refunded": _handle_charge_refunded,
    "charge.dispute.created": _handle_dispute_created,
}
