"""§F3 (Handoff #18): the billing endpoints under /api/billing/.

Rule 1 applies with extra force here: nothing the browser sends about a
price, amount, product or entitlement is trusted. /checkout/ takes a
product KEY and resolves everything server-side from billing/catalog.
The webhook verifies the Stripe-Signature on the RAW body before parsing.
THE SUCCESS PAGE GRANTS NOTHING — fulfillment happens only in the webhook;
/status/?session= is what the success page polls.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .catalog import (
    billing_enabled,
    configure_stripe,
    get_product,
    price_id_for,
    public_products,
)
from .models import (
    BillingAccount,
    Entitlement,
    Purchase,
    PurchaseStatus,
    StripeEvent,
    StripeEventStatus,
)
from .services import ProcessingError, process_event

logger = logging.getLogger(__name__)

BILLING_OFF = {
    "detail": "Billing isn't configured on this server.",
    "code": "billing_not_configured",
}


def _stripe_error_details(exc) -> str:
    """One grep-able line of Stripe's OWN complaint (safe to log: error
    codes and messages, never keys). Works for any exception class."""
    parts = [type(exc).__name__]
    for attr in ("code", "user_message"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={value}")
    body = getattr(exc, "json_body", None)
    if isinstance(body, dict):
        message = (body.get("error") or {}).get("message")
        if message:
            parts.append(str(message))
    if len(parts) == 1:
        parts.append(str(exc))
    return " · ".join(parts)


class CheckoutThrottle(UserRateThrottle):
    rate = "10/min"
    scope = "billing_checkout"


def _get_or_create_customer(stripe, user) -> BillingAccount:
    account = BillingAccount.objects.filter(user=user).first()
    if account is not None:
        return account
    customer = stripe.Customer.create(
        email=user.email,
        name=user.display_name or "",
        metadata={"drinkquiry_user_id": str(user.id)},
    )
    account, _ = BillingAccount.objects.get_or_create(
        user=user, defaults={"stripe_customer_id": customer["id"]}
    )
    return account


class CheckoutView(APIView):
    """POST /api/billing/checkout/ {product: <key>[, entitlement: <id>]} →
    {url}. `entitlement` only for the (dark) reactivation keys."""

    permission_classes = (permissions.IsAuthenticated,)
    throttle_classes = (CheckoutThrottle,)

    def post(self, request):
        if not billing_enabled():
            return Response(BILLING_OFF, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        key = str(request.data.get("product") or "")
        entry = get_product(key)
        if entry is None:
            return Response({"product": ["Unknown product."]}, status=status.HTTP_400_BAD_REQUEST)
        if not entry["enabled"]:
            # C-4: Venue Tournament ships as architecture only.
            return Response(
                {"detail": "This product isn't available yet.", "code": "product_coming_soon"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        price_id = price_id_for(key)
        if not price_id:
            return Response(BILLING_OFF, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        reactivates = None
        if entry.get("reactivation_of"):
            ent_id = request.data.get("entitlement")
            reactivates = Entitlement.objects.filter(
                pk=ent_id, user=request.user, kind=entry["reactivation_of"]
            ).first()
            if reactivates is None:
                return Response(
                    {"entitlement": ["Pick which pack this reactivates."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        stripe = configure_stripe()
        base = settings.FRONTEND_BASE_URL
        try:
            account = _get_or_create_customer(stripe, request.user)
        except Exception as exc:  # noqa: BLE001 — surfaced below, generic to the browser
            logger.exception(
                "Stripe checkout FAILED at Customer step (product=%s): %s",
                key, _stripe_error_details(exc),
            )
            return Response(
                {"detail": "Couldn't start checkout — please try again in a moment.",
                 "code": "stripe_error"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        try:
            with transaction.atomic():
                purchase = Purchase.objects.create(
                    user=request.user,
                    product_key=key,
                    reactivates=reactivates,
                    metadata={"reactivates": reactivates.pk} if reactivates else {},
                )
                session = stripe.checkout.Session.create(
                    mode=entry["mode"],
                    customer=account.stripe_customer_id,
                    line_items=[{"price": price_id, "quantity": 1}],
                    success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{base}/pricing",
                    automatic_tax={"enabled": settings.STRIPE_AUTOMATIC_TAX},
                    metadata={
                        "drinkquiry_user_id": str(request.user.id),
                        "product_key": key,
                        "purchase_type": entry["mode"],
                        # §F4(d) (#19): a lost-row REACTIVATION must be
                        # rebuildable from metadata alone (services.py's
                        # _recover_purchase_from_metadata).
                        **({"reactivates": str(reactivates.pk)} if reactivates else {}),
                    },
                    idempotency_key=f"dq-checkout-{purchase.pk}",
                )
                purchase.stripe_checkout_session_id = session["id"]
                purchase.save(update_fields=["stripe_checkout_session_id"])
        except Exception as exc:  # noqa: BLE001
            # The #1 real-world cause here is a test/live MODE MISMATCH:
            # an sk_test_ key with a price id copied from live mode (or a
            # different Stripe account) → "No such price". Run
            # `manage.py billing_check` server-side for a full diagnosis.
            logger.exception(
                "Stripe checkout FAILED at Session step (product=%s, price=%s): %s",
                key, price_id, _stripe_error_details(exc),
            )
            return Response(
                {"detail": "Couldn't start checkout — please try again in a moment.",
                 "code": "stripe_error"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"url": session["url"]})


@method_decorator(csrf_exempt, name="dispatch")
class WebhookView(APIView):
    """POST /api/billing/webhook/ — unauthenticated, signature-verified on
    the RAW body (never parse first). First act: StripeEvent dedupe."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    def post(self, request):
        if not settings.STRIPE_WEBHOOK_SECRET:
            return Response(BILLING_OFF, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        import stripe as stripe_sdk

        payload = request.body  # RAW bytes — the signature covers these
        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            stripe_sdk.Webhook.construct_event(
                payload, signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except Exception:
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)
        # construct_event verified the signature over these exact bytes; the
        # processor works on the plain-JSON parse of them (StripeObject's
        # lazy attribute access doesn't survive dict() coercion cleanly, and
        # the stored payload should be plain JSON for §F9's retry anyway).
        import json

        try:
            event = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

        record, created = StripeEvent.objects.get_or_create(
            stripe_event_id=str(event.get("id") or ""),
            defaults={"event_type": str(event.get("type") or ""), "payload": event},
        )
        if not created:
            return Response({"received": True, "duplicate": True})

        try:
            outcome = process_event(event)
        except ProcessingError as exc:
            record.status = StripeEventStatus.FAILED
            record.error = str(exc)
            record.save(update_fields=["status", "error"])
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:  # noqa: BLE001 — recorded + 500 so Stripe retries
            logger.exception("Webhook processing crashed (%s)", event.get("type"))
            record.status = StripeEventStatus.FAILED
            record.error = repr(exc)
            record.save(update_fields=["status", "error"])
            return Response(
                {"detail": "Processing failed."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        record.status = (
            StripeEventStatus.SKIPPED if outcome == "skipped" else StripeEventStatus.PROCESSED
        )
        record.processed_at = timezone.now()
        record.save(update_fields=["status", "processed_at"])
        return Response({"received": True})


class StatusView(APIView):
    """GET /api/billing/status/[?session=cs_…] — the account's billing
    truth. PINNED shape (exact-shape test):
    {entitlements, subscriptions, purchases, session} — session null unless
    ?session= names one of the caller's checkout sessions."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        from .access import entitlement_usage_summary

        subs = []
        for sub in request.user.stripe_subscriptions.all().order_by("-created_at", "-id"):
            entry = get_product(sub.product_key) or {}
            subs.append(
                {
                    "product_key": sub.product_key,
                    "name": entry.get("display_name", sub.product_key),
                    "status": sub.status,
                    "cancel_at_period_end": sub.cancel_at_period_end,
                    "current_period_end": sub.current_period_end.isoformat()
                    if sub.current_period_end
                    else None,
                    "grace_period_ends_at": sub.grace_period_ends_at.isoformat()
                    if sub.grace_period_ends_at
                    else None,
                }
            )
        purchases = []
        for p in request.user.purchases.all().order_by("-created_at", "-id")[:20]:
            entry = get_product(p.product_key) or {}
            purchases.append(
                {
                    "product_key": p.product_key,
                    "name": entry.get("display_name", p.product_key),
                    "status": p.status,
                    "amount_total": p.amount_total,
                    "currency": p.currency,
                    "purchased_at": p.purchased_at.isoformat() if p.purchased_at else None,
                }
            )
        session_block = None
        if session_id := request.query_params.get("session"):
            purchase = Purchase.objects.filter(
                user=request.user, stripe_checkout_session_id=session_id
            ).first()
            if purchase is not None:
                session_block = {
                    "product_key": purchase.product_key,
                    "status": purchase.status,
                    "paid": purchase.status == PurchaseStatus.PAID,
                }
        return Response(
            {
                "entitlements": entitlement_usage_summary(request.user),
                "subscriptions": subs,
                "purchases": purchases,
                "session": session_block,
            }
        )


class ProductsView(APIView):
    """GET /api/billing/products/ — public display catalog. PINNED shape:
    a bare array of {key, name, price, interval, blurb, coming_soon}."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    def get(self, request):
        return Response(public_products())


class PortalView(APIView):
    """POST /api/billing/portal/ → {url}. Requires a BillingAccount (you
    can't manage billing you've never had)."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        if not billing_enabled():
            return Response(BILLING_OFF, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        account = BillingAccount.objects.filter(user=request.user).first()
        if account is None:
            return Response(
                {"detail": "No billing account yet — nothing to manage."},
                status=status.HTTP_404_NOT_FOUND,
            )
        stripe = configure_stripe()
        try:
            session = stripe.billing_portal.Session.create(
                customer=account.stripe_customer_id,
                return_url=f"{settings.FRONTEND_BASE_URL}/profile",
            )
        except Exception:
            logger.exception("Stripe portal session creation failed")
            return Response(
                {"detail": "Couldn't open the billing portal — please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"url": session["url"]})
