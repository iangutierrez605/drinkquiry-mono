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
        Question.objects.create(
            category=cat,
            owner=None,
            question_text=f"Q{i}?",
            answer=f"A{i}",
            difficulty=(i % 5) + 1,
            visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED,
        )
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
                "category": category_id,
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
