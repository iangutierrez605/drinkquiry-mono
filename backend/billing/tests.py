"""Billing tests (Handoff #18 §F2/§F3).

Sandbox rule 10: the suite NEVER talks to Stripe — SDK outbound calls
(Customer/Session/portal create) are mocked; webhook signature verification
is tested FOR REAL by signing payloads locally with the documented scheme
(Stripe-Signature: t=<ts>,v1=HMAC_SHA256(secret, f"{ts}.{raw_body}")) and
asserting stripe.Webhook.construct_event accepts them and rejects tampering.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from accounts.quotas import limits_for
from trivia.models import Category, Question

from .models import (
    Entitlement,
    EntitlementKind,
    Purchase,
    PurchaseStatus,
    StripeEvent,
    StripeEventStatus,
    Subscription,
)

SECRET = "whsec_test_secret"

BILLING_ON = dict(
    STRIPE_SECRET_KEY="sk_test_x",
    STRIPE_WEBHOOK_SECRET=SECRET,
    STRIPE_PRICE_PARTY_GAME_50="price_party",
    STRIPE_PRICE_BIG_GAME_100="price_big",
    STRIPE_PRICE_VENUE_MONTHLY="price_venue",
    STRIPE_PRICE_TOURNAMENT_PASS="price_pass",
    STRIPE_PRICE_PARTY_GAME_REACTIVATION="price_party_re",
    STRIPE_PRICE_BIG_GAME_REACTIVATION="price_big_re",
)


def sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event_body(event_id: str, etype: str, obj: dict, created: int | None = None) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": etype,
            "created": created or int(time.time()),
            "data": {"object": obj},
        }
    ).encode()


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer@test.com", "sturdy-pass-123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def hook(self, body: bytes, signature: str | None = None):
        return self.client.post(
            "/api/billing/webhook/",
            data=body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature if signature is not None else sign(body),
        )

    def make_purchase(self, key="party_game_50", session="cs_test_1", **kwargs):
        return Purchase.objects.create(
            user=self.user, product_key=key, stripe_checkout_session_id=session, **kwargs
        )

    def completed_session(self, session="cs_test_1", mode="payment", paid=True, **extra):
        obj = {
            "id": session,
            "object": "checkout.session",
            "mode": mode,
            "payment_status": "paid" if paid else "unpaid",
            "payment_intent": "pi_test_1",
            "amount_total": 999,
            "currency": "usd",
            "customer": "cus_test_1",
        }
        obj.update(extra)
        return obj


class BillingModelTests(Base):
    def test_stripe_event_id_unique(self):
        StripeEvent.objects.create(stripe_event_id="evt_1", event_type="x")
        with self.assertRaises(Exception):
            StripeEvent.objects.create(stripe_event_id="evt_1", event_type="x")

    def test_entitlement_needs_exactly_one_source(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Entitlement.objects.create(user=self.user, kind=EntitlementKind.PARTY_PACK)

    def test_one_entitlement_per_purchase(self):
        from django.db import IntegrityError, transaction

        p = self.make_purchase()
        Entitlement.objects.create(user=self.user, kind=EntitlementKind.PARTY_PACK, source_purchase=p)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Entitlement.objects.create(
                user=self.user, kind=EntitlementKind.PARTY_PACK, source_purchase=p
            )

    def test_is_active_truth_table(self):
        now = timezone.now()
        p = self.make_purchase()
        ent = Entitlement.objects.create(
            user=self.user,
            kind=EntitlementKind.PARTY_PACK,
            source_purchase=p,
            active_from=now - timedelta(days=1),
            active_until=now + timedelta(days=29),
        )
        self.assertTrue(ent.is_active)
        ent.active_until = now - timedelta(minutes=1)
        self.assertFalse(ent.is_active)  # expired window
        ent.active_until = now + timedelta(days=1)
        ent.active_from = now + timedelta(hours=1)
        self.assertFalse(ent.is_active)  # not yet started

        sub = Subscription.objects.create(
            user=self.user, product_key="venue_monthly", stripe_subscription_id="sub_1",
            status="active",
        )
        vent = Entitlement.objects.create(
            user=self.user, kind=EntitlementKind.VENUE, source_subscription=sub
        )
        self.assertTrue(vent.is_active)
        sub.status = "past_due"
        sub.grace_period_ends_at = now + timedelta(days=3)
        sub.save()
        vent.refresh_from_db()
        self.assertTrue(vent.is_active)  # past_due within grace
        sub.grace_period_ends_at = now - timedelta(minutes=1)
        sub.save()
        vent.refresh_from_db()
        self.assertFalse(vent.is_active)  # grace over
        sub.status = "canceled"
        sub.save()
        vent.refresh_from_db()
        self.assertFalse(vent.is_active)


@override_settings(**BILLING_ON)
class CatalogTests(Base):
    def test_price_resolution_and_enabled(self):
        from .catalog import billing_enabled, price_id_for

        self.assertTrue(billing_enabled())
        self.assertEqual(price_id_for("party_game_50"), "price_party")
        self.assertEqual(price_id_for("venue_tournament_monthly"), "")  # unset env

    def test_public_products_shape(self):
        r = self.client.get("/api/billing/products/")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        keys = {row["key"] for row in rows}
        self.assertNotIn("party_game_reactivation", keys)  # dark
        for row in rows:
            self.assertEqual(
                set(row), {"key", "name", "price", "interval", "blurb", "coming_soon"}
            )
        coming = {row["key"]: row["coming_soon"] for row in rows}
        self.assertTrue(coming["venue_tournament_monthly"])  # C-4


class BillingDisabledTests(Base):
    def test_keyless_checkout_and_portal_503(self):
        r = self.client.post("/api/billing/checkout/", {"product": "party_game_50"}, format="json")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "billing_not_configured")
        r = self.client.post("/api/billing/portal/")
        self.assertEqual(r.status_code, 503)

    def test_keyless_webhook_503(self):
        r = self.hook(b"{}", signature="t=1,v1=junk")
        self.assertEqual(r.status_code, 503)


@override_settings(**BILLING_ON)
class CheckoutViewTests(Base):
    def _mock_stripe(self):
        customer = mock.patch("stripe.Customer.create", return_value={"id": "cus_test_1"})
        session = mock.patch(
            "stripe.checkout.Session.create",
            return_value={"id": "cs_new_1", "url": "https://checkout.stripe.test/cs_new_1"},
        )
        return customer, session

    def test_unknown_product_400(self):
        r = self.client.post("/api/billing/checkout/", {"product": "nope"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_disabled_product_rejected(self):
        r = self.client.post(
            "/api/billing/checkout/", {"product": "venue_tournament_monthly"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "product_coming_soon")

    def test_happy_path_creates_pending_purchase(self):
        cust, sess = self._mock_stripe()
        with cust as c_mock, sess as s_mock:
            r = self.client.post(
                "/api/billing/checkout/", {"product": "party_game_50"}, format="json"
            )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"url": "https://checkout.stripe.test/cs_new_1"})
        purchase = Purchase.objects.get(user=self.user)
        self.assertEqual(purchase.status, PurchaseStatus.PENDING)
        self.assertEqual(purchase.stripe_checkout_session_id, "cs_new_1")
        kwargs = s_mock.call_args.kwargs
        # The PRICE ID came from settings, server-side; the browser sent a key.
        self.assertEqual(kwargs["line_items"], [{"price": "price_party", "quantity": 1}])
        self.assertEqual(kwargs["mode"], "payment")
        self.assertEqual(kwargs["metadata"]["product_key"], "party_game_50")
        self.assertIn("session_id={CHECKOUT_SESSION_ID}", kwargs["success_url"])
        self.assertTrue(c_mock.called)

    def test_two_simultaneous_checkouts_two_pending(self):
        cust, sess = self._mock_stripe()
        with cust, sess as s_mock:
            s_mock.side_effect = [
                {"id": "cs_a", "url": "https://x/a"},
                {"id": "cs_b", "url": "https://x/b"},
            ]
            self.client.post("/api/billing/checkout/", {"product": "party_game_50"}, format="json")
            self.client.post("/api/billing/checkout/", {"product": "party_game_50"}, format="json")
        self.assertEqual(
            Purchase.objects.filter(user=self.user, status=PurchaseStatus.PENDING).count(), 2
        )

    def test_reactivation_needs_valid_entitlement(self):
        r = self.client.post(
            "/api/billing/checkout/", {"product": "party_game_reactivation"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        p = self.make_purchase(session="cs_seed")
        ent = Entitlement.objects.create(
            user=self.user, kind=EntitlementKind.PARTY_PACK, source_purchase=p
        )
        cust, sess = self._mock_stripe()
        with cust, sess:
            r = self.client.post(
                "/api/billing/checkout/",
                {"product": "party_game_reactivation", "entitlement": ent.pk},
                format="json",
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Purchase.objects.get(stripe_checkout_session_id="cs_new_1").reactivates, ent)


@override_settings(**BILLING_ON)
class WebhookSignatureTests(Base):
    """Rule 10: the REAL verification path, offline."""

    def test_valid_signature_accepted(self):
        self.make_purchase()
        body = event_body("evt_ok", "checkout.session.completed", self.completed_session())
        r = self.hook(body)
        self.assertEqual(r.status_code, 200, r.content)
        record = StripeEvent.objects.get(stripe_event_id="evt_ok")
        self.assertEqual(record.status, "processed")

    def test_tampered_body_rejected(self):
        body = event_body("evt_t", "checkout.session.completed", self.completed_session())
        good = sign(body)
        r = self.hook(body + b" ", signature=good)  # body changed after signing
        self.assertEqual(r.status_code, 400)
        self.assertEqual(StripeEvent.objects.count(), 0)

    def test_wrong_secret_rejected(self):
        body = event_body("evt_w", "checkout.session.completed", self.completed_session())
        r = self.hook(body, signature=sign(body, secret="whsec_other"))
        self.assertEqual(r.status_code, 400)

    def test_stale_timestamp_rejected(self):
        body = event_body("evt_s", "checkout.session.completed", self.completed_session())
        stale = sign(body, ts=int(time.time()) - 3600)  # construct_event default tolerance 300s
        r = self.hook(body, signature=stale)
        self.assertEqual(r.status_code, 400)

    def test_replay_changes_nothing(self):
        self.make_purchase()
        body = event_body("evt_r", "checkout.session.completed", self.completed_session())
        self.assertEqual(self.hook(body).status_code, 200)
        counts = (
            Purchase.objects.count(),
            Entitlement.objects.count(),
            Category.objects.count(),
            len(mail.outbox),
        )
        r = self.hook(body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("duplicate"))
        self.assertEqual(
            counts,
            (
                Purchase.objects.count(),
                Entitlement.objects.count(),
                Category.objects.count(),
                len(mail.outbox),
            ),
        )


@override_settings(**BILLING_ON)
class FulfillmentTests(Base):
    def test_paid_checkout_grants_pack_without_auto_category(self):
        # #19.1 (owner ruling): fulfillment grants the ENTITLEMENT only —
        # no auto-created starter category. Buyers make their own via the
        # pack lane on /create (now surfaced in the category form); the
        # email's guidance line points them there.
        purchase = self.make_purchase()
        body = event_body("evt_f1", "checkout.session.completed", self.completed_session())
        self.assertEqual(self.hook(body).status_code, 200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PurchaseStatus.PAID)
        self.assertEqual(purchase.amount_total, 999)
        self.assertEqual(purchase.stripe_payment_intent_id, "pi_test_1")
        ent = Entitlement.objects.get(source_purchase=purchase)
        self.assertEqual(ent.kind, EntitlementKind.PARTY_PACK)
        self.assertEqual(ent.question_limit, 50)
        self.assertTrue(ent.is_active)
        self.assertAlmostEqual(
            (ent.active_until - ent.active_from).days, 30, delta=1
        )
        self.assertEqual(Category.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Party Game", mail.outbox[0].subject)
        self.assertNotIn("starter category", mail.outbox[0].body)
        self.assertIn("Your content", mail.outbox[0].body)
        # Owner ruling: the app IS the receipt — money facts ride the email.
        self.assertIn("$9.99 USD", mail.outbox[0].body)
        self.assertIn("pi_test_1", mail.outbox[0].body)
        self.assertNotIn("Stripe", mail.outbox[0].body)

    def test_unpaid_completion_grants_nothing_until_async_success(self):
        purchase = self.make_purchase()
        body = event_body(
            "evt_u1", "checkout.session.completed", self.completed_session(paid=False)
        )
        self.assertEqual(self.hook(body).status_code, 200)
        self.assertEqual(Entitlement.objects.count(), 0)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PurchaseStatus.PENDING)
        body = event_body(
            "evt_u2", "checkout.session.async_payment_succeeded", self.completed_session()
        )
        self.assertEqual(self.hook(body).status_code, 200)
        self.assertEqual(Entitlement.objects.count(), 1)

    def test_expired_session_marks_failed(self):
        purchase = self.make_purchase()
        body = event_body("evt_x", "checkout.session.expired", {"id": "cs_test_1"})
        self.assertEqual(self.hook(body).status_code, 200)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, PurchaseStatus.FAILED)

    def test_subscription_checkout_creates_sub_and_venue_entitlement(self):
        self.make_purchase(key="venue_monthly")
        body = event_body(
            "evt_sub1",
            "checkout.session.completed",
            self.completed_session(mode="subscription", subscription="sub_new_1"),
        )
        self.assertEqual(self.hook(body).status_code, 200)
        sub = Subscription.objects.get(stripe_subscription_id="sub_new_1")
        self.assertEqual(sub.user, self.user)
        ent = Entitlement.objects.get(source_subscription=sub)
        self.assertEqual(ent.kind, EntitlementKind.VENUE)
        self.assertIsNone(ent.active_until)  # follows the subscription
        self.assertTrue(ent.is_active)
        self.assertEqual(len(mail.outbox), 1)

    def _venue(self):
        self.make_purchase(key="venue_monthly", session="cs_v")
        body = event_body(
            "evt_v0",
            "checkout.session.completed",
            self.completed_session(session="cs_v", mode="subscription", subscription="sub_v"),
        )
        self.hook(body)
        mail.outbox.clear()
        return Subscription.objects.get(stripe_subscription_id="sub_v")

    def test_payment_failed_sets_grace_then_lapses(self):
        sub = self._venue()
        invoice = {"id": "in_1", "subscription": "sub_v", "lines": {"data": []}}
        body = event_body("evt_pf", "invoice.payment_failed", invoice)
        self.assertEqual(self.hook(body).status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "past_due")
        self.assertIsNotNone(sub.grace_period_ends_at)
        self.assertEqual(len(mail.outbox), 1)  # portal-hint email
        ent = Entitlement.objects.get(source_subscription=sub)
        self.assertTrue(ent.is_active)  # access continues through grace
        sub.grace_period_ends_at = timezone.now() - timedelta(minutes=1)
        sub.save()
        ent.refresh_from_db()
        self.assertFalse(ent.is_active)  # then goes false on its own

    def test_invoice_paid_restores_and_clears_grace(self):
        sub = self._venue()
        self.hook(
            event_body(
                "evt_pf2", "invoice.payment_failed", {"id": "in_2", "subscription": "sub_v"}
            )
        )
        end = int(time.time()) + 30 * 86400
        invoice = {
            "id": "in_3",
            "subscription": "sub_v",
            "amount_paid": 2999,
            "currency": "usd",
            "lines": {"data": [{"period": {"start": int(time.time()), "end": end}}]},
        }
        mail.outbox.clear()
        self.assertEqual(self.hook(event_body("evt_ip", "invoice.paid", invoice)).status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "active")
        self.assertIsNone(sub.grace_period_ends_at)
        self.assertEqual(int(sub.current_period_end.timestamp()), end)
        # Renewal receipt (owner self-receipting ruling): one per paid charge.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Receipt", mail.outbox[0].subject)
        self.assertIn("$29.99 USD", mail.outbox[0].body)
        # …and a $0 invoice sends nothing.
        zero = dict(invoice, id="in_4", amount_paid=0)
        self.hook(event_body("evt_ip0", "invoice.paid", zero))
        self.assertEqual(len(mail.outbox), 1)

    def test_subscription_updated_out_of_order_skipped(self):
        sub = self._venue()
        now = int(time.time())
        newer = {
            "id": "sub_v",
            "status": "active",
            "cancel_at_period_end": True,
            "items": {"data": [{"current_period_end": now + 86400, "price": {"id": "price_venue"}}]},
        }
        self.hook(event_body("evt_new", "customer.subscription.updated", newer, created=now))
        sub.refresh_from_db()
        self.assertTrue(sub.cancel_at_period_end)
        older = {"id": "sub_v", "status": "active", "cancel_at_period_end": False}
        r = self.hook(
            event_body("evt_old", "customer.subscription.updated", older, created=now - 500)
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_old").status, "skipped")
        sub.refresh_from_db()
        self.assertTrue(sub.cancel_at_period_end)  # the stale flip did NOT apply

    def test_subscription_deleted_preserves_content(self):
        sub = self._venue()
        cat = Category.objects.create(owner=self.user, name="Venue night")
        self.hook(
            event_body(
                "evt_del", "customer.subscription.deleted",
                {"id": "sub_v", "canceled_at": int(time.time())},
                created=int(time.time()) + 10,
            )
        )
        sub.refresh_from_db()
        self.assertEqual(sub.status, "canceled")
        ent = Entitlement.objects.get(source_subscription=sub)
        self.assertFalse(ent.is_active)
        cat.refresh_from_db()
        self.assertIsNone(cat.deleted_at)  # deletion of nothing, ever

    def test_refund_unused_revokes_used_flags(self):
        purchase = self.make_purchase()
        self.hook(event_body("evt_p", "checkout.session.completed", self.completed_session()))
        ent = Entitlement.objects.get(source_purchase=purchase)
        self.hook(event_body("evt_rf", "charge.refunded", {"payment_intent": "pi_test_1"}))
        purchase.refresh_from_db()
        ent.refresh_from_db()
        self.assertEqual(purchase.status, PurchaseStatus.REFUNDED)
        self.assertFalse(ent.is_active)  # unused → window ended now
        # A SECOND pack, substantially used → flagged, not revoked.
        p2 = self.make_purchase(session="cs_2")
        self.hook(
            event_body(
                "evt_p2", "checkout.session.completed",
                self.completed_session(session="cs_2", payment_intent="pi_2"),
            )
        )
        ent2 = Entitlement.objects.get(source_purchase=p2)
        # #19.1: no auto starter — the "used pack" fixture makes its own
        # bound category, exactly as a real buyer now does via /create.
        bound2 = Category.objects.create(owner=self.user, name="Round 1", entitlement=ent2)
        Question.objects.create(owner=self.user, question_text="Q?", answer="A").categories.set(
            [bound2]
        )
        from games.services import create_game

        create_game(
            host=self.user, mode="drinks", category_ids=[bound2.pk], questions_per_category=1
        )
        self.hook(event_body("evt_d2", "charge.dispute.created", {"payment_intent": "pi_2"}))
        ent2.refresh_from_db()
        self.assertTrue(ent2.is_active)  # NOT revoked
        self.assertEqual(ent2.metadata.get("manual_review"), "disputed")

    def test_reactivation_extends_window(self):
        p = self.make_purchase(session="cs_seed")
        ent = Entitlement.objects.create(
            user=self.user,
            kind=EntitlementKind.PARTY_PACK,
            source_purchase=p,
            active_from=timezone.now() - timedelta(days=40),
            active_until=timezone.now() - timedelta(days=10),
        )
        self.assertFalse(ent.is_active)
        re_purchase = self.make_purchase(
            key="party_game_reactivation", session="cs_re", reactivates=ent
        )
        self.hook(
            event_body(
                "evt_re", "checkout.session.completed", self.completed_session(session="cs_re")
            )
        )
        ent.refresh_from_db()
        self.assertTrue(ent.is_active)
        self.assertAlmostEqual((ent.active_until - timezone.now()).days, 30, delta=1)
        re_purchase.refresh_from_db()
        self.assertEqual(re_purchase.status, PurchaseStatus.PAID)
        # No NEW entitlement was minted.
        self.assertEqual(Entitlement.objects.count(), 1)


class StripeEventAdminRetryTests(Base):
    def test_retry_failed_processes_and_grants_idempotently(self):
        # §F6 (Handoff #19): a valid stored payload whose first processing
        # "crashed" (row marked failed) retries to a grant; a SECOND retry
        # of the same payload grants nothing twice (handler idempotence).
        from unittest.mock import MagicMock, patch

        from django.contrib import admin as dj_admin

        from .admin import StripeEventAdmin

        self.make_purchase()
        payload = json.loads(
            event_body("evt_retry1", "checkout.session.completed", self.completed_session())
        )
        record = StripeEvent.objects.create(
            stripe_event_id="evt_retry1",
            event_type="checkout.session.completed",
            status=StripeEventStatus.FAILED,
            error="simulated crash",
            payload=payload,
        )
        # An already-processed row rides along to prove the failed-only guard.
        bystander = StripeEvent.objects.create(
            stripe_event_id="evt_retry2",
            event_type="checkout.session.completed",
            status=StripeEventStatus.PROCESSED,
            payload=payload,
        )
        model_admin = StripeEventAdmin(StripeEvent, dj_admin.site)
        with patch.object(StripeEventAdmin, "message_user") as message_user:
            model_admin.retry_failed(MagicMock(), StripeEvent.objects.all())
        record.refresh_from_db()
        self.assertEqual(record.status, StripeEventStatus.PROCESSED)
        self.assertEqual(record.error, "")
        self.assertIsNotNone(record.processed_at)
        bystander.refresh_from_db()  # guard: non-failed rows untouched
        self.assertIsNone(bystander.processed_at)
        self.assertEqual(Entitlement.objects.count(), 1)
        message_user.assert_called_once()
        # Round two: re-mark failed, retry again → still exactly one grant.
        record.status = StripeEventStatus.FAILED
        record.save(update_fields=["status"])
        with patch.object(StripeEventAdmin, "message_user"):
            model_admin.retry_failed(MagicMock(), StripeEvent.objects.all())
        self.assertEqual(Entitlement.objects.count(), 1)
        self.assertEqual(
            Purchase.objects.get(stripe_checkout_session_id="cs_test_1").status,
            PurchaseStatus.PAID,
        )


@override_settings(**BILLING_ON)
class ForeignSessionTests(Base):
    """§F4 (Handoff #19): the webhook must never 500 on sessions it can't
    own. Foreign sessions (Dashboard payment links — no Drinkquiry
    metadata) SKIP with a 200 so Stripe stops retrying a grant that can
    never happen; sessions WITH our metadata but a lost pending row are
    rebuilt from the signature-verified metadata and fulfilled."""

    def test_foreign_completed_session_skips_with_200(self):
        # The not-a-500 assertion is the point: pre-#19 an unknown session
        # raised ProcessingError → 500 → Stripe retried forever on every
        # payment-link sale.
        body = event_body("evt_fs1", "checkout.session.completed", self.completed_session())
        res = self.hook(body)
        self.assertEqual(res.status_code, 200, res.content)
        record = StripeEvent.objects.get(stripe_event_id="evt_fs1")
        self.assertEqual(record.status, StripeEventStatus.SKIPPED)
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_our_metadata_rebuilds_and_fulfills(self):
        # Process death between Stripe accepting the session and our commit:
        # no pending row, but the session carries our signature-verified
        # metadata — the row is rebuilt and the grant lands exactly once.
        session = self.completed_session(
            metadata={
                "drinkquiry_user_id": str(self.user.id),
                "product_key": "party_game_50",
                "purchase_type": "payment",
            }
        )
        body = event_body("evt_fs2", "checkout.session.completed", session)
        self.assertEqual(self.hook(body).status_code, 200)
        record = StripeEvent.objects.get(stripe_event_id="evt_fs2")
        self.assertEqual(record.status, StripeEventStatus.PROCESSED)
        purchase = Purchase.objects.get(stripe_checkout_session_id="cs_test_1")
        self.assertEqual(purchase.user, self.user)
        self.assertEqual(purchase.status, PurchaseStatus.PAID)
        self.assertTrue(purchase.metadata.get("recovered_from_metadata"))
        ent = Entitlement.objects.get(source_purchase=purchase)
        self.assertEqual(ent.kind, EntitlementKind.PARTY_PACK)
        self.assertEqual(Category.objects.count(), 0)  # #19.1: no auto category
        self.assertEqual(len(mail.outbox), 1)  # the one confirmation — nothing doubled

    def test_foreign_subscription_session_skips(self):
        session = self.completed_session(mode="subscription", subscription="sub_foreign_1")
        body = event_body("evt_fs3", "checkout.session.completed", session)
        self.assertEqual(self.hook(body).status_code, 200)
        self.assertEqual(
            StripeEvent.objects.get(stripe_event_id="evt_fs3").status,
            StripeEventStatus.SKIPPED,
        )
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(Entitlement.objects.count(), 0)


@override_settings(**BILLING_ON)
class StatusViewTests(Base):
    def test_exact_shape_and_session_polling(self):
        purchase = self.make_purchase()
        r = self.client.get("/api/billing/status/?session=cs_test_1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), {"entitlements", "subscriptions", "purchases", "session"})
        self.assertEqual(
            body["session"], {"product_key": "party_game_50", "status": "pending", "paid": False}
        )
        self.hook(event_body("evt_sp", "checkout.session.completed", self.completed_session()))
        body = self.client.get("/api/billing/status/?session=cs_test_1").json()
        self.assertTrue(body["session"]["paid"])
        ent_row = body["entitlements"][0]
        self.assertEqual(
            set(ent_row),
            {
                "id", "kind", "is_active", "active_from", "active_until",
                "question_limit", "questions_used", "game_limit",
            },
        )
        self.assertEqual(ent_row["questions_used"], 0)
        # A foreign session id resolves to null, not someone else's purchase.
        other = User.objects.create_user("other@test.com", "sturdy-pass-123")
        purchase.user = other
        purchase.save(update_fields=["user"])
        body = self.client.get("/api/billing/status/?session=cs_test_1").json()
        self.assertIsNone(body["session"])


class QuotaUnionTests(Base):
    """§F2's union + §F6 coexistence, from the quotas side."""

    def _active_venue(self):
        sub = Subscription.objects.create(
            user=self.user, product_key="venue_monthly", stripe_subscription_id="sub_u", status="active"
        )
        return Entitlement.objects.create(
            user=self.user, kind=EntitlementKind.VENUE, source_subscription=sub
        )

    def test_venue_unions_over_free(self):
        self.assertEqual(limits_for(self.user)["categories"], 0)
        self._active_venue()
        limits = limits_for(self.user)
        self.assertEqual(limits["categories"], 25)
        self.assertIsNone(limits["questions"])  # total uncapped; 100-ACTIVE gates
        self.assertEqual(limits["storage_bytes"], 500 * 1024 * 1024)
        self.assertEqual(limits["tournaments"], 0)  # venue ≠ tournaments

    def test_manual_plan_wins_where_bigger(self):
        self.user.plan = "creator"
        self.user.save()
        self._active_venue()
        limits = limits_for(self.user)
        self.assertEqual(limits["categories"], 25)
        self.assertIsNone(limits["questions"])  # union: venue's None beats 500
        self.assertEqual(limits["tournaments"], 25)  # creator's own number stands

    def test_lapsed_venue_contributes_nothing(self):
        ent = self._active_venue()
        ent.source_subscription.status = "canceled"
        ent.source_subscription.save()
        self.assertEqual(limits_for(self.user)["categories"], 0)

    def test_active_pass_unions_tournaments(self):
        p = self.make_purchase(key="tournament_pass", session="cs_tp")
        Entitlement.objects.create(
            user=self.user,
            kind=EntitlementKind.TOURNAMENT_PASS,
            source_purchase=p,
            active_until=timezone.now() + timedelta(days=30),
        )
        self.assertEqual(limits_for(self.user)["tournaments"], 1)
