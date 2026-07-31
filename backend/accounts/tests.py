"""Paid-tier tests (Handoff #3 §D6).

Run: `python manage.py test accounts trivia games`
Works against SQLite or, with DATABASE_URL set, real Postgres — quota logic is
plain counting, but running it once through Postgres before shipping keeps the
environment-parity rule honest.
"""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from trivia.models import Category, ModerationStatus, Question, Visibility

TINY_LIMITS = {
    "free": {"games_per_month": 2, "categories": 0, "questions": 0},
    "creator": {"games_per_month": None, "categories": 2, "questions": 3},
}


def make_official_category(name="Movies", questions=3):
    cat = Category.objects.create(
        owner=None, name=name, visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED
    )
    for i in range(questions):
        question = Question.objects.create(
            owner=None,
            question_text=f"Q{i}?",
            answer=f"A{i}",
            difficulty=(i % 5) + 1,
            visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED,
        )
        question.categories.add(cat)  # §F (Handoff #10): categories are M2M
    return cat


class QuotaTestBase(APITestCase):
    def setUp(self):
        self.free = User.objects.create_user("free@test.com", "sturdy-pass-123", display_name="Free")
        self.paid = User.objects.create_user(
            "paid@test.com", "sturdy-pass-123", display_name="Paid", plan="creator"
        )

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def create_category(self, name):
        return self.client.post("/api/categories/", {"name": name, "visibility": "private"})

    def create_question(self, category_id, text="What?"):
        return self.client.post(
            "/api/questions/",
            {
                # §K1 (#11): the single-`category` alias is gone — the real
                # contract is `categories` (a form list posts as repeated keys).
                "categories": [category_id],
                "question_text": text,
                "answer": "That",
                "difficulty": 2,
                "media_type": "none",
                "visibility": "private",
            },
        )

    def assert_quota_403(self, response, code, used, limit):
        self.assertEqual(response.status_code, 403, response.content)
        body = response.json()
        self.assertEqual(body["code"], code)
        self.assertEqual(body["used"], used)
        self.assertEqual(body["limit"], limit)
        self.assertIn("detail", body)


class ContentQuotaTests(QuotaTestBase):
    def test_free_user_blocked_with_quota_shaped_403(self):
        self.auth(self.free)
        r = self.create_category("Nope")
        self.assert_quota_403(r, "quota_categories", used=0, limit=0)

        official = make_official_category()
        r = self.create_question(official.id)
        self.assert_quota_403(r, "quota_questions", used=0, limit=0)

    def test_creator_within_limits_succeeds(self):
        self.auth(self.paid)
        r = self.create_category("Mine")
        self.assertEqual(r.status_code, 201, r.content)
        r = self.create_question(r.json()["id"])
        self.assertEqual(r.status_code, 201, r.content)

    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_category_limit_boundary(self):
        self.auth(self.paid)
        self.assertEqual(self.create_category("One").status_code, 201)
        self.assertEqual(self.create_category("Two").status_code, 201)
        self.assert_quota_403(self.create_category("Three"), "quota_categories", used=2, limit=2)

    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_question_limit_boundary(self):
        self.auth(self.paid)
        cat_id = self.create_category("Mine").json()["id"]
        for i in range(3):
            self.assertEqual(self.create_question(cat_id, f"Q{i}?").status_code, 201)
        self.assert_quota_403(self.create_question(cat_id, "Q3?"), "quota_questions", used=3, limit=3)

    def test_expired_plan_behaves_as_free(self):
        self.paid.plan_expires_at = timezone.now() - timedelta(days=1)
        self.paid.save()
        self.assertFalse(self.paid.is_creator)
        self.auth(self.paid)
        self.assert_quota_403(self.create_category("Late"), "quota_categories", used=0, limit=0)

    def test_unexpired_plan_still_creator(self):
        self.paid.plan_expires_at = timezone.now() + timedelta(days=30)
        self.paid.save()
        self.assertTrue(self.paid.is_creator)
        self.auth(self.paid)
        self.assertEqual(self.create_category("On time").status_code, 201)

    def test_downgraded_user_cannot_edit_but_error_is_not_quota_shaped(self):
        # Writes other than create still go through the plain IsCreator gate.
        self.auth(self.paid)
        cat_id = self.create_category("Mine").json()["id"]
        self.paid.plan = "free"
        self.paid.save()
        self.auth(self.paid)
        r = self.client.patch(f"/api/categories/{cat_id}/", {"description": "edit"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("code", r.json())


class GameQuotaTests(QuotaTestBase):
    def setUp(self):
        super().setUp()
        self.official = make_official_category(questions=3)

    def create_game(self):
        return self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [self.official.id], "questions_per_category": 1},
            format="json",
        )

    def test_default_settings_leave_hosting_unlimited(self):
        self.auth(self.free)
        for _ in range(3):
            self.assertEqual(self.create_game().status_code, 201)

    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_game_limit_boundary_for_free(self):
        self.auth(self.free)
        self.assertEqual(self.create_game().status_code, 201)
        self.assertEqual(self.create_game().status_code, 201)
        self.assert_quota_403(self.create_game(), "quota_games", used=2, limit=2)

    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_creator_games_unlimited(self):
        self.auth(self.paid)
        for _ in range(3):
            self.assertEqual(self.create_game().status_code, 201)


class ProfileTests(QuotaTestBase):
    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_profile_usage_matches_reality(self):
        self.auth(self.paid)
        cat_id = self.create_category("Mine").json()["id"]
        self.create_question(cat_id)
        official = make_official_category(questions=3)
        self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [official.id], "questions_per_category": 1},
            format="json",
        )
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["plan"], "creator")
        self.assertIsNone(p["plan_expires_at"])
        self.assertTrue(p["is_creator"])
        self.assertEqual(p["usage"]["categories"], {"used": 1, "limit": 2})
        self.assertEqual(p["usage"]["questions"], {"used": 1, "limit": 3})
        self.assertEqual(p["usage"]["games_this_month"], {"used": 1, "limit": None})

    def test_expired_plan_reports_free_with_free_limits(self):
        self.paid.plan_expires_at = timezone.now() - timedelta(days=1)
        self.paid.save()
        self.auth(self.paid)
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["plan"], "free")
        self.assertFalse(p["is_creator"])
        self.assertEqual(p["usage"]["categories"]["limit"], 0)

    def test_free_profile_shape(self):
        self.auth(self.free)
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["plan"], "free")
        self.assertFalse(p["is_creator"])
        self.assertEqual(p["usage"]["questions"], {"used": 0, "limit": 0})
        self.assertIsNone(p["usage"]["games_this_month"]["limit"])  # hosting ungated today


# ---------------------------------------------------------------------------
# Handoff #9 §J — per-user limit overrides + the staff user-management API
# ---------------------------------------------------------------------------

from unittest.mock import patch

from django.core import mail
from django.core.cache import cache

from accounts import quotas


@override_settings(PLAN_LIMITS=TINY_LIMITS)
class LimitOverrideTests(QuotaTestBase):
    def test_limits_for_merge_semantics(self):
        # replace / null-unlimited / fall-through, in one dict
        self.paid.limit_overrides = {"questions": 10, "categories": None}
        limits = quotas.limits_for(self.paid)
        self.assertEqual(limits["questions"], 10)          # replaced
        self.assertIsNone(limits["categories"])            # null = unlimited
        self.assertIsNone(limits["games_per_month"])       # missing key → plan default
        # untouched user: pure plan defaults
        self.assertEqual(quotas.limits_for(self.free)["categories"], 0)

    def test_free_user_with_override_hits_the_overridden_boundary(self):
        cat = make_official_category(questions=0)
        self.free.limit_overrides = {"questions": 2}
        self.free.save(update_fields=["limit_overrides"])
        self.auth(self.free)
        for i in range(2):
            self.assertEqual(self.create_question(cat.id, f"Q{i}?").status_code, 201)
        res = self.create_question(cat.id, "One too many?")
        # The structured 403 carries the OVERRIDDEN limit, not the plan's 0.
        self.assert_quota_403(res, "quota_questions", used=2, limit=2)

    def test_profile_usage_reflects_overrides(self):
        self.free.limit_overrides = {"questions": 7, "storage_bytes": None}
        self.free.save(update_fields=["limit_overrides"])
        self.auth(self.free)
        usage = self.client.get("/api/auth/profile/").data["usage"]
        self.assertEqual(usage["questions"]["limit"], 7)
        self.assertIsNone(usage["storage"]["limit"])
        self.assertEqual(usage["categories"]["limit"], 0)  # untouched key

    def test_overrides_survive_plan_lapse(self):
        """Pinned as intended (§J4): an override is a grant to the USER, not
        to the plan — it still applies after the paid plan lapses to free."""
        self.paid.plan_expires_at = timezone.now() - timedelta(days=1)
        self.paid.limit_overrides = {"questions": 5}
        self.paid.save(update_fields=["plan_expires_at", "limit_overrides"])
        self.assertEqual(self.paid.effective_plan, "free")
        limits = quotas.limits_for(self.paid)
        self.assertEqual(limits["questions"], 5)           # override on top of FREE base
        self.assertEqual(limits["categories"], 0)          # free's default, not creator's

    def test_validate_overrides(self):
        quotas.validate_overrides({"questions": 0, "storage_bytes": None})  # fine
        for bad in ({"bogus": 1}, {"questions": -1}, {"questions": True}, {"questions": "9"}, ["questions"]):
            with self.assertRaises(ValueError):
                quotas.validate_overrides(bad)


class AdminUserApiTests(QuotaTestBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user("staff@test.com", "sturdy-pass-123", is_staff=True)

    def test_staff_only(self):
        self.auth(self.free)
        self.assertEqual(self.client.get("/api/moderation/users/").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/moderation/users/{self.free.id}/", {}).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/moderation/users/").status_code, 401)
        self.assertEqual(self.client.patch(f"/api/moderation/users/{self.free.id}/", {}).status_code, 401)

    def test_list_shape_search_and_filters(self):
        self.auth(self.staff)
        res = self.client.get("/api/moderation/users/")
        self.assertEqual(res.status_code, 200)
        data = res.data
        self.assertTrue(set(data) >= {"results", "count", "next", "previous"})
        row = next(r for r in data["results"] if r["email"] == "paid@test.com")
        expected_keys = {
            "id", "email", "display_name", "plan", "effective_plan",
            "plan_expires_at", "is_staff", "date_joined", "limit_overrides", "usage",
            # §H (Handoff #11): brand oversight fields (shape extended, not
            # mutated — the C4-safe direction).
            "brand_name", "brand_logo",
        }
        self.assertEqual(set(row), expected_keys)
        self.assertEqual(set(row["usage"]), {"games_this_month", "categories", "questions", "storage"})
        # search by email fragment and by display name
        res = self.client.get("/api/moderation/users/?search=paid@")
        self.assertEqual([r["email"] for r in res.data["results"]], ["paid@test.com"])
        res = self.client.get("/api/moderation/users/?search=Free")
        self.assertEqual([r["email"] for r in res.data["results"]], ["free@test.com"])
        res = self.client.get("/api/moderation/users/?plan=creator")
        self.assertEqual({r["email"] for r in res.data["results"]}, {"paid@test.com"})
        res = self.client.get("/api/moderation/users/?is_staff=true")
        self.assertEqual({r["email"] for r in res.data["results"]}, {"staff@test.com"})
        self.assertEqual(self.client.get("/api/moderation/users/?plan=platinum").status_code, 400)

    def test_patch_round_trip(self):
        self.auth(self.staff)
        expiry = (timezone.now() + timedelta(days=14)).isoformat()
        res = self.client.patch(
            f"/api/moderation/users/{self.free.id}/",
            {"plan": "creator", "plan_expires_at": expiry, "limit_overrides": {"questions": 50}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["plan"], "creator")
        self.assertEqual(res.data["effective_plan"], "creator")
        self.assertEqual(res.data["limit_overrides"], {"questions": 50})
        self.free.refresh_from_db()
        self.assertEqual(self.free.plan, "creator")
        self.assertEqual(self.free.limit_overrides, {"questions": 50})
        # a lapsed demo reads creator-but-effectively-free in the same row
        past = (timezone.now() - timedelta(days=1)).isoformat()
        res = self.client.patch(f"/api/moderation/users/{self.free.id}/",
                                {"plan_expires_at": past}, format="json")
        self.assertEqual(res.data["plan"], "creator")
        self.assertEqual(res.data["effective_plan"], "free")

    def test_patch_validation(self):
        self.auth(self.staff)
        url = f"/api/moderation/users/{self.free.id}/"
        # unknown override key / bad value → 400
        res = self.client.patch(url, {"limit_overrides": {"bogus": 1}}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(url, {"limit_overrides": {"questions": -3}}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(url, {"plan": "platinum"}, format="json")
        self.assertEqual(res.status_code, 400)
        # is_staff (or anything else) in the body → 400, pinned: silently
        # ignoring a privilege-looking field would be worse than refusing.
        res = self.client.patch(url, {"is_staff": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.free.refresh_from_db()
        self.assertFalse(self.free.is_staff)
        res = self.client.patch(url, {"email": "hijack@test.com"}, format="json")
        self.assertEqual(res.status_code, 400)


# ---------------------------------------------------------------------------
# Handoff #9 §K1/§K2 — password flows
# ---------------------------------------------------------------------------

from knox.models import AuthToken  # noqa: E402


class PasswordFlowTestBase(QuotaTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()  # the forgot-cooldown must never leak between tests

    def knox_login(self, email, password):
        res = self.client.post("/api/auth/login/", {"email": email, "password": password})
        self.assertEqual(res.status_code, 200, res.content)
        return res.data["token"]

    def bearer(self, token):
        return {"HTTP_AUTHORIZATION": f"Token {token}"}


class ForgotPasswordTests(PasswordFlowTestBase):
    def test_identical_200s_for_existing_and_unknown_email(self):
        r1 = self.client.post("/api/auth/password/forgot/", {"email": "free@test.com"})
        r2 = self.client.post("/api/auth/password/forgot/", {"email": "nobody@test.com"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data, r2.data)  # no enumeration
        self.assertEqual(len(mail.outbox), 1)  # one real send, zero for the ghost
        self.assertEqual(mail.outbox[0].to, ["free@test.com"])
        self.assertIn("/reset-password?uid=", mail.outbox[0].body)

    def test_cooldown_skips_the_send_silently(self):
        r1 = self.client.post("/api/auth/password/forgot/", {"email": "free@test.com"})
        r2 = self.client.post("/api/auth/password/forgot/", {"email": "free@test.com"})
        self.assertEqual(r1.data, r2.data)  # body identical either way
        self.assertEqual(len(mail.outbox), 1)  # second send skipped

    def test_token_round_trip_resets_and_kills_all_sessions(self):
        token = self.knox_login("free@test.com", "sturdy-pass-123")
        self.client.post("/api/auth/password/forgot/", {"email": "free@test.com"})
        body = mail.outbox[0].body
        query = body.split("/reset-password?")[1].split()[0]
        params = dict(pair.split("=", 1) for pair in query.split("&"))
        res = self.client.post("/api/auth/password/reset/",
                               {"uid": params["uid"], "token": params["token"],
                                "new_password": "fresh-pass-456"})
        self.assertEqual(res.status_code, 200, res.content)
        # every Knox session is dead
        self.assertEqual(self.client.get("/api/auth/profile/", **self.bearer(token)).status_code, 401)
        self.assertEqual(AuthToken.objects.filter(user=self.free).count(), 0)
        # old password out, new password in
        self.assertEqual(
            self.client.post("/api/auth/login/", {"email": "free@test.com", "password": "sturdy-pass-123"}).status_code,
            400,
        )
        self.knox_login("free@test.com", "fresh-pass-456")

    def test_bad_or_expired_token_400s_generically(self):
        self.client.post("/api/auth/password/forgot/", {"email": "free@test.com"})
        body = mail.outbox[0].body
        query = body.split("/reset-password?")[1].split()[0]
        params = dict(pair.split("=", 1) for pair in query.split("&"))
        res = self.client.post("/api/auth/password/reset/",
                               {"uid": params["uid"], "token": "garbage-token",
                                "new_password": "fresh-pass-456"})
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("free@test.com", str(res.data))  # generic message
        res = self.client.post("/api/auth/password/reset/",
                               {"uid": "!!!!", "token": params["token"], "new_password": "fresh-pass-456"})
        self.assertEqual(res.status_code, 400)
        # weak password still rejected AFTER a valid token
        res = self.client.post("/api/auth/password/reset/",
                               {"uid": params["uid"], "token": params["token"], "new_password": "123"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("new_password", res.data)


class ChangePasswordTests(PasswordFlowTestBase):
    def test_wrong_current_400(self):
        token = self.knox_login("free@test.com", "sturdy-pass-123")
        res = self.client.post("/api/auth/password/change/",
                               {"current_password": "wrong", "new_password": "fresh-pass-456"},
                               **self.bearer(token))
        self.assertEqual(res.status_code, 400)
        self.assertIn("current_password", res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_change_keeps_current_session_kills_others_and_notifies(self):
        token_here = self.knox_login("free@test.com", "sturdy-pass-123")
        token_elsewhere = self.knox_login("free@test.com", "sturdy-pass-123")
        res = self.client.post("/api/auth/password/change/",
                               {"current_password": "sturdy-pass-123", "new_password": "fresh-pass-456"},
                               **self.bearer(token_here))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.client.get("/api/auth/profile/", **self.bearer(token_here)).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/profile/", **self.bearer(token_elsewhere)).status_code, 401)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password was changed", mail.outbox[0].subject.lower() + mail.outbox[0].body.lower())
        self.knox_login("free@test.com", "fresh-pass-456")

    def test_anonymous_401(self):
        res = self.client.post("/api/auth/password/change/",
                               {"current_password": "x", "new_password": "y"})
        self.assertEqual(res.status_code, 401)

    def test_email_failure_never_breaks_the_change(self):
        token = self.knox_login("free@test.com", "sturdy-pass-123")
        with patch("accounts.emails.send_mail", side_effect=RuntimeError("ESP down")):
            res = self.client.post("/api/auth/password/change/",
                                   {"current_password": "sturdy-pass-123", "new_password": "fresh-pass-456"},
                                   **self.bearer(token))
        self.assertEqual(res.status_code, 200, res.content)
        self.knox_login("free@test.com", "fresh-pass-456")


# ---------------------------------------------------------------------------
# Handoff #10 §I2 — auth-surface throttling
# ---------------------------------------------------------------------------

def tiny_rates(testcase):
    """Pin every auth throttle to 2/min for one test. NOT override_settings
    on REST_FRAMEWORK: SimpleRateThrottle captures THROTTLE_RATES as a CLASS
    attribute at import time, so a settings override never reaches an
    already-imported throttle — patching the classes' `rate` (which
    short-circuits get_rate()) is the reliable seam."""
    from unittest.mock import patch as _patch

    from accounts.throttling import (
        LoginRateThrottle,
        PasswordForgotRateThrottle,
        PasswordResetRateThrottle,
        RegisterRateThrottle,
    )

    for cls in (LoginRateThrottle, RegisterRateThrottle, PasswordForgotRateThrottle, PasswordResetRateThrottle):
        patcher = _patch.object(cls, "rate", "2/min", create=True)  # `rate` is normally set per-instance
        patcher.start()
        testcase.addCleanup(patcher.stop)


class AuthThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.addCleanup(cache.clear)
        tiny_rates(self)
        User.objects.create_user("throttle@test.com", "sturdy-pass-123")

    def login(self):
        return self.client.post(
            "/api/auth/login/", {"email": "throttle@test.com", "password": "wrong-on-purpose"}
        )

    def test_third_login_within_the_window_is_429(self):
        self.assertEqual(self.login().status_code, 400)
        self.assertEqual(self.login().status_code, 400)
        res = self.login()
        self.assertEqual(res.status_code, 429)
        # DRF's default body — a NEW status, not a mutated shape (C4).
        self.assertIn("detail", res.json())

    def test_scopes_do_not_collide_across_endpoints(self):
        self.login()
        self.login()
        self.assertEqual(self.login().status_code, 429)  # login exhausted...
        res = self.client.post(
            "/api/auth/register/",
            {"email": "fresh@test.com", "password": "sturdy-pass-123", "display_name": "F"},
        )
        self.assertIn(res.status_code, (200, 201), res.content)  # ...register untouched
        res = self.client.post("/api/auth/password/forgot/", {"email": "throttle@test.com"})
        self.assertEqual(res.status_code, 200)  # ...and so is forgot

    def test_snapshot_and_join_stay_unthrottled(self):
        # The throttles are per-view, NOT a global default: hammering login
        # must never 429 the public polling snapshot (a bar of phones behind
        # one NAT IP is the normal case).
        self.login()
        self.login()
        self.assertEqual(self.login().status_code, 429)
        for _ in range(5):
            res = self.client.get("/api/games/NOSUCH1/")
            self.assertEqual(res.status_code, 404)  # not 429
        # §G1 (Handoff #12): the public category browse joins the
        # deliberately-unthrottled list — it's the marketing surface.
        for _ in range(5):
            res = self.client.get("/api/categories/public/")
            self.assertEqual(res.status_code, 200)  # not 429

    def test_forgot_throttle_keeps_bodies_identical_before_the_limit(self):
        # §L: cooldown/throttle independence — a throttled forgot is a
        # DIFFERENT STATUS, but every 200 keeps the pinned enumeration-proof
        # body (real vs unknown email identical).
        real = self.client.post("/api/auth/password/forgot/", {"email": "throttle@test.com"})
        fake = self.client.post("/api/auth/password/forgot/", {"email": "ghost@test.com"})
        self.assertEqual(real.status_code, fake.status_code, 200)
        self.assertEqual(real.json(), fake.json())
        self.assertEqual(self.client.post("/api/auth/password/forgot/", {"email": "x@test.com"}).status_code, 429)


class ThrottleIdentTests(APITestCase):
    """§I2's proxy problem: getting the client ident wrong throttles the
    whole site as one IP. Pinned under both settings."""

    def make_request(self, **meta):
        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().post("/api/auth/login/")
        request.META.update(meta)
        return request

    def ident(self, request):
        from accounts.throttling import LoginRateThrottle

        return LoginRateThrottle().get_ident(request)

    @override_settings(DJANGO_BEHIND_PROXY=True)
    def test_behind_proxy_reads_the_last_forwarded_hop(self):
        request = self.make_request(
            REMOTE_ADDR="10.0.0.2",  # the proxy
            HTTP_X_FORWARDED_FOR="203.0.113.7, 198.51.100.9",
        )
        # NUM_PROXIES=1 semantics: ONE trusted hop appended the last entry.
        self.assertEqual(self.ident(request), "198.51.100.9")

    @override_settings(DJANGO_BEHIND_PROXY=True)
    def test_behind_proxy_without_header_falls_back_to_remote_addr(self):
        request = self.make_request(REMOTE_ADDR="10.0.0.2")
        self.assertEqual(self.ident(request), "10.0.0.2")

    @override_settings(DJANGO_BEHIND_PROXY=False)
    def test_direct_mode_ignores_spoofable_forwarded_headers(self):
        request = self.make_request(
            REMOTE_ADDR="203.0.113.7",
            HTTP_X_FORWARDED_FOR="1.2.3.4",  # client-supplied, untrusted here
        )
        self.assertEqual(self.ident(request), "203.0.113.7")


# ---------------------------------------------------------------------------
# Handoff #10 §I3 — health check
# ---------------------------------------------------------------------------

class HealthCheckTests(APITestCase):
    def test_ok_shape_exactly(self):
        res = self.client.get("/api/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})  # smoke pins this too

    def test_no_auth_needed_and_stale_tokens_ignored(self):
        res = self.client.get("/api/health/", HTTP_AUTHORIZATION="Token garbage")
        self.assertEqual(res.status_code, 200)

    @override_settings(REDIS_URL="redis://example.invalid:6379/0")
    def test_cache_failure_degrades_to_503(self):
        with patch("config.health.cache") as mocked:
            mocked.set.side_effect = RuntimeError("redis down")
            res = self.client.get("/api/health/")
        self.assertEqual(res.status_code, 503)
        body = res.json()
        self.assertEqual(body["status"], "degraded")
        self.assertIn("cache", body)

    def test_database_failure_degrades_to_503(self):
        with patch("config.health.connection") as mocked:
            mocked.cursor.side_effect = RuntimeError("db gone")
            res = self.client.get("/api/health/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["status"], "degraded")


# ---------------------------------------------------------------------------
# §H (Handoff #11) — venue branding: profile writes, quota, plan gating,
# snapshot serving, staff oversight
# ---------------------------------------------------------------------------
import io
import os
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

BRAND_TMP_MEDIA = tempfile.mkdtemp(prefix="dq-test-brand-media-")


def brand_png(kb=8):
    """Incompressible noise PNG (tiny dims, under the resize threshold) so
    the stored size ≈ the requested size — same trick as trivia's quota
    tests."""
    img = Image.frombytes("L", (256, kb * 4), os.urandom(256 * kb * 4))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=BRAND_TMP_MEDIA)
class BrandingTests(QuotaTestBase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(BRAND_TMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def patch_profile(self, data):
        return self.client.patch("/api/auth/profile/", data, format="multipart")

    def make_game(self, host):
        from games.services import create_game
        from trivia.models import Category, Question

        cat = Category.objects.create(owner=None, name=f"BrandCat-{Category.objects.count()}")
        for i in range(2):
            q = Question.objects.create(
                owner=None, question_text=f"B{i}?", answer=f"A{i}", difficulty=1
            )
            q.categories.add(cat)
        return create_game(host=host, mode="points", category_ids=[cat.id], questions_per_category=2)

    def snapshot(self, game):
        res = self.client.get(f"/api/games/{game.code}/")
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_creator_sets_both_fields_multipart(self):
        self.auth(self.paid)
        res = self.patch_profile({"brand_name": "THE KINGS ARMS", "brand_logo": brand_png()})
        self.assertEqual(res.status_code, 200, res.content)
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.brand_name, "THE KINGS ARMS")
        self.assertTrue(self.paid.brand_logo)
        self.assertGreater(self.paid.brand_logo_bytes, 0)
        self.assertEqual(self.paid.brand_logo_bytes, self.paid.brand_logo.size)
        # The profile READS the fields back.
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["brand_name"], "THE KINGS ARMS")
        self.assertTrue(p["brand_logo"])

    def test_free_plan_write_plain_403_reads_fine(self):
        self.auth(self.free)
        res = self.patch_profile({"brand_name": "SNEAKY"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json(), {"detail": "Branding is part of the creator plan."})
        res = self.patch_profile({"brand_logo": brand_png()})
        self.assertEqual(res.status_code, 403)
        # Reads are fine — and a brand-free PATCH still works for free users.
        self.assertEqual(self.client.get("/api/auth/profile/").status_code, 200)
        res = self.client.patch("/api/auth/profile/", {"display_name": "Still Free"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_register_cannot_set_branding(self):
        self.client.force_authenticate(user=None)
        res = self.client.post(
            "/api/auth/register/",
            {"email": "venue@test.com", "password": "sturdy-pass-123", "brand_name": "SNEAKY"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(User.objects.get(email="venue@test.com").brand_name, "")

    def test_storage_quota_applies_and_frees_on_clear(self):
        from accounts.quotas import storage_bytes_used

        self.auth(self.paid)
        self.assertEqual(self.patch_profile({"brand_logo": brand_png(8)}).status_code, 200)
        self.paid.refresh_from_db()
        used = self.paid.brand_logo_bytes
        self.assertEqual(storage_bytes_used(self.paid), used)
        # A cap below the next upload → the standard structured quota_storage 403.
        with override_settings(
            PLAN_LIMITS={
                "free": {"games_per_month": None, "categories": 0, "questions": 0, "storage_bytes": 0},
                "creator": {"games_per_month": None, "categories": 25, "questions": 500, "storage_bytes": used + 100},
            }
        ):
            res = self.patch_profile({"brand_logo": brand_png(8)})
            self.assert_quota_403(res, "quota_storage", used, used + 100)
        # Clearing frees the bytes (the column goes to 0 with it).
        res = self.patch_profile({"brand_logo_clear": "true"})
        self.assertEqual(res.status_code, 200, res.content)
        self.paid.refresh_from_db()
        self.assertFalse(self.paid.brand_logo)
        self.assertEqual(self.paid.brand_logo_bytes, 0)
        self.assertEqual(storage_bytes_used(self.paid), 0)

    def test_snapshot_brand_shape_and_gating(self):
        self.auth(self.paid)
        game = self.make_game(self.paid)
        # Nothing set → null.
        self.assertIsNone(self.snapshot(game)["brand"])
        self.assertEqual(self.patch_profile({"brand_name": "THE KINGS ARMS", "brand_logo": brand_png()}).status_code, 200)
        snap = self.snapshot(game)
        self.assertEqual(set(snap["brand"]), {"name", "logo"})
        self.assertEqual(snap["brand"]["name"], "THE KINGS ARMS")
        self.assertIn("/media/brands/", snap["brand"]["logo"])
        # A free host's game never carries a brand.
        free_game = self.make_game(self.free)
        self.assertIsNone(self.snapshot(free_game)["brand"])

    def test_plan_lapse_hides_brand_but_fields_persist(self):
        self.auth(self.paid)
        game = self.make_game(self.paid)
        self.assertEqual(self.patch_profile({"brand_name": "THE KINGS ARMS"}).status_code, 200)
        self.assertEqual(self.snapshot(game)["brand"], {"name": "THE KINGS ARMS", "logo": None})
        # Lapse: brand disappears from NEW snapshots; the fields remain.
        self.paid.plan_expires_at = timezone.now() - timedelta(days=1)
        self.paid.save(update_fields=["plan_expires_at"])
        self.assertIsNone(self.snapshot(game)["brand"])
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.brand_name, "THE KINGS ARMS")
        # Flip back → it returns (§N step 5).
        self.paid.plan_expires_at = None
        self.paid.save(update_fields=["plan_expires_at"])
        self.assertEqual(self.snapshot(game)["brand"], {"name": "THE KINGS ARMS", "logo": None})

    def test_staff_patch_whitelist_extended_not_loosened(self):
        staff = User.objects.create_user("brandstaff@test.com", "sturdy-pass-123", is_staff=True)
        self.paid.brand_name = "THE KINGS ARMS"
        self.paid.brand_logo = brand_png()
        self.paid.save()
        self.assertGreater(self.paid.brand_logo_bytes, 0)
        self.auth(staff)
        url = f"/api/moderation/users/{self.paid.id}/"
        # The two NEW keys are accepted…
        res = self.client.patch(url, {"brand_name": "", "brand_logo_clear": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.brand_name, "")
        self.assertFalse(self.paid.brand_logo)
        self.assertEqual(self.paid.brand_logo_bytes, 0)
        # …and everything else still 400s (the pinned whitelist shape).
        res = self.client.patch(url, {"is_staff": True}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(url, {"brand_logo": "hax"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_admin_list_shows_brand_fields(self):
        staff = User.objects.create_user("brandstaff2@test.com", "sturdy-pass-123", is_staff=True)
        self.paid.brand_name = "THE KINGS ARMS"
        self.paid.save(update_fields=["brand_name"])
        self.auth(staff)
        res = self.client.get("/api/moderation/users/?search=paid@")
        row = res.data["results"][0]
        self.assertEqual(row["brand_name"], "THE KINGS ARMS")
        self.assertIn("brand_logo", row)


# ---------------------------------------------------------------------------
# §F (Handoff #12) — bot gates on the public register surface
# ---------------------------------------------------------------------------
class RegisterHoneypotTests(APITestCase):
    """§F1: the decoy `website` field. A non-empty value is a bot → the NEW
    pinned vague 400 (C4: added body, nothing mutated) and NO user row.
    Empty/absent proceed exactly as today, so every existing client (and the
    smoke's register calls, C12) is untouched."""

    FIELDS = {"email": "human@test.com", "password": "sturdy-pass-123", "display_name": "H"}

    def test_filled_honeypot_is_the_exact_vague_400_and_creates_nothing(self):
        res = self.client.post(
            "/api/auth/register/", {**self.FIELDS, "website": "https://spam.example"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.content)
        # Exact pinned body — deliberately vague: never names the field,
        # never hints the mechanism.
        self.assertEqual(res.json(), {"detail": "Registration failed."})
        self.assertFalse(User.objects.filter(email="human@test.com").exists())

    def test_whitespace_only_honeypot_reads_as_empty(self):
        res = self.client.post(
            "/api/auth/register/", {**self.FIELDS, "website": "   "}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_empty_honeypot_registers_as_today(self):
        res = self.client.post(
            "/api/auth/register/", {**self.FIELDS, "website": ""}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(User.objects.filter(email="human@test.com").exists())

    def test_absent_honeypot_registers_as_today(self):
        res = self.client.post("/api/auth/register/", self.FIELDS, format="json")
        self.assertEqual(res.status_code, 201, res.content)


class TurnstileTests(APITestCase):
    """§F2: env-gated challenge, OFF by default (the suite runs keyless).
    ON is simulated with override_settings + a mocked requests.post — the
    same seam the Resend tests use for their backend."""

    def setUp(self):
        cache.clear()  # register-throttle counters live in the cache (C10)
        self.addCleanup(cache.clear)

    FIELDS = {"email": "turn@test.com", "password": "sturdy-pass-123", "display_name": "T"}
    TURNSTILE_400 = {"detail": "Verification failed — please try again."}

    def register(self, extra=None):
        return self.client.post("/api/auth/register/", {**self.FIELDS, **(extra or {})}, format="json")

    # --- OFF (the default): the feature is entirely absent -----------------
    def test_off_by_default_and_a_stray_token_is_ignored(self):
        res = self.register({"turnstile_token": "unsolicited"})
        self.assertEqual(res.status_code, 201, res.content)

    # --- ON: override the secret, mock the verify --------------------------
    @override_settings(TURNSTILE_SECRET_KEY="test-secret")
    def test_on_missing_token_is_the_exact_400(self):
        res = self.register()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json(), self.TURNSTILE_400)
        self.assertFalse(User.objects.filter(email="turn@test.com").exists())

    @override_settings(TURNSTILE_SECRET_KEY="test-secret")
    def test_on_verify_false_is_400(self):
        from unittest import mock

        fake = mock.Mock()
        fake.json.return_value = {"success": False, "error-codes": ["invalid-input-response"]}
        with mock.patch("accounts.turnstile.requests.post", return_value=fake) as post:
            res = self.register({"turnstile_token": "bad-token"})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json(), self.TURNSTILE_400)
        # The verify hit Cloudflare's endpoint with our secret + the token.
        args, kwargs = post.call_args
        self.assertIn("challenges.cloudflare.com", args[0])
        self.assertEqual(kwargs["data"]["secret"], "test-secret")
        self.assertEqual(kwargs["data"]["response"], "bad-token")

    @override_settings(TURNSTILE_SECRET_KEY="test-secret")
    def test_on_verify_true_registers(self):
        from unittest import mock

        fake = mock.Mock()
        fake.json.return_value = {"success": True}
        with mock.patch("accounts.turnstile.requests.post", return_value=fake):
            res = self.register({"turnstile_token": "solved"})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(User.objects.filter(email="turn@test.com").exists())

    @override_settings(TURNSTILE_SECRET_KEY="test-secret")
    def test_on_verify_timeout_fails_closed(self):
        # Documented decision: a verify-endpoint outage reads as "not
        # verified" — a signup can retry; a bot flood cannot be undone.
        from unittest import mock

        import requests as _requests

        with mock.patch(
            "accounts.turnstile.requests.post", side_effect=_requests.Timeout("verify hung")
        ):
            res = self.register({"turnstile_token": "solved-but-unverifiable"})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json(), self.TURNSTILE_400)
        self.assertFalse(User.objects.filter(email="turn@test.com").exists())

    @override_settings(TURNSTILE_SECRET_KEY="test-secret")
    def test_honeypot_still_wins_over_turnstile(self):
        # Gate order: the free check runs first — a bot that filled the
        # honeypot never costs us a verify round-trip.
        from unittest import mock

        with mock.patch("accounts.turnstile.requests.post") as post:
            res = self.register({"website": "spam", "turnstile_token": "whatever"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"detail": "Registration failed."})
        post.assert_not_called()
