"""Moderation review queue + bulk upload tests (Handoff #4 §H, #5 §H).

Covers the review queue, bulk CSV v1, §F auto-created categories, and §G
zip-with-media. Run: `python manage.py test accounts trivia games` (and, per the
environment-parity rule, once through docker compose against Postgres before
shipping).
"""
import io
import os
import pathlib
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from trivia.models import Category, ModerationStatus, Question, QuestionReport, ReportStatus, Visibility

TINY_LIMITS = {
    "free": {"games_per_month": None, "categories": 0, "questions": 0},
    "creator": {"games_per_month": None, "categories": 25, "questions": 3},
}

HEADER = "category,question_text,answer,difficulty,visibility\n"


def csv_file(text, name="upload.csv", encode="utf-8"):
    return SimpleUploadedFile(name, text.encode(encode) if isinstance(text, str) else text, content_type="text/csv")


def make_category(owner, name, *, public_approved=False):
    return Category.objects.create(
        owner=owner,
        name=name,
        visibility=Visibility.PUBLIC if public_approved else Visibility.PRIVATE,
        moderation_status=ModerationStatus.APPROVED if public_approved else ModerationStatus.NOT_SUBMITTED,
    )


def make_question(owner, category, text, *, status=ModerationStatus.NOT_SUBMITTED, visibility=Visibility.PRIVATE, note=""):
    # §F (Handoff #10): categories are M2M — `category` may be one Category
    # or a list of them; the helper's call sites stayed unchanged.
    question = Question.objects.create(
        owner=owner,
        question_text=text,
        answer="A",
        difficulty=1,
        visibility=visibility,
        moderation_status=status,
        moderation_note=note,
    )
    question.categories.set(category if isinstance(category, (list, tuple)) else [category])
    return question


class BaseCase(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff@test.com", "sturdy-pass-123", is_staff=True)
        self.creator = User.objects.create_user("creator@test.com", "sturdy-pass-123", plan="creator")
        self.other = User.objects.create_user("other@test.com", "sturdy-pass-123", plan="creator")
        self.free = User.objects.create_user("free@test.com", "sturdy-pass-123")

    def auth(self, user):
        self.client.force_authenticate(user=user)


# ---------------------------------------------------------------------------
# §F — moderation review queue
# ---------------------------------------------------------------------------

class ModerationApiTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(self.creator, "Movies")
        # Two pending questions from different users; created_at ordering is
        # auto_now_add so creation order == oldest-first order.
        self.q_old = make_question(
            self.creator, self.cat, "Oldest?", status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC
        )
        self.q_new = make_question(
            self.other, self.cat, "Newest?", status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC
        )
        self.pending_cat = Category.objects.create(
            owner=self.other, name="Pending Cat", visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.PENDING,
        )

    def test_non_staff_forbidden_everywhere(self):
        endpoints = [
            ("get", "/api/moderation/categories/"),
            ("get", "/api/moderation/questions/"),
            ("get", "/api/moderation/counts/"),
            ("post", f"/api/moderation/questions/{self.q_old.id}/approve/"),
            ("post", f"/api/moderation/questions/{self.q_old.id}/reject/"),
            ("post", f"/api/moderation/categories/{self.pending_cat.id}/approve/"),
            ("post", f"/api/moderation/categories/{self.pending_cat.id}/reject/"),
        ]
        for user in (self.creator, self.free):
            self.auth(user)
            for method, url in endpoints:
                res = getattr(self.client, method)(url)
                self.assertEqual(res.status_code, 403, f"{user.email} {method} {url}: {res.status_code}")
        # Nothing changed under all that hammering.
        self.q_old.refresh_from_db()
        self.assertEqual(self.q_old.moderation_status, ModerationStatus.PENDING)

    def test_staff_list_pending_oldest_first_with_owner_context(self):
        self.auth(self.staff)
        res = self.client.get("/api/moderation/questions/")
        self.assertEqual(res.status_code, 200)
        rows = res.data["results"]
        self.assertEqual([r["id"] for r in rows], [self.q_old.id, self.q_new.id])
        self.assertEqual(rows[0]["owner_email"], "creator@test.com")
        self.assertEqual(rows[1]["owner_email"], "other@test.com")
        self.assertEqual(rows[0]["category_names"], ["Movies"])  # §F4: sorted list now
        self.assertIn("answer", rows[0])  # reviewers need the answer

    def test_status_filter_and_invalid_status(self):
        make_question(
            self.other, self.cat, "Rejected one", status=ModerationStatus.REJECTED,
            visibility=Visibility.PUBLIC, note="Nope",
        )
        self.auth(self.staff)
        res = self.client.get("/api/moderation/questions/?status=rejected")
        self.assertEqual([r["question_text"] for r in res.data["results"]], ["Rejected one"])
        self.assertEqual(self.client.get("/api/moderation/questions/?status=bogus").status_code, 400)

    def test_approve_flips_status_and_clears_note(self):
        self.q_old.moderation_note = "old feedback"
        self.q_old.save(update_fields=["moderation_note"])
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.q_old.id}/approve/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["moderation_status"], "approved")
        self.assertEqual(res.data["moderation_note"], "")
        self.q_old.refresh_from_db()
        self.assertEqual(self.q_old.moderation_status, ModerationStatus.APPROVED)
        self.assertEqual(self.q_old.moderation_note, "")

    def test_reject_requires_note_and_writes_it(self):
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.q_old.id}/reject/", {"note": "  "})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(f"/api/moderation/questions/{self.q_old.id}/reject/", {"note": "Too blurry"})
        self.assertEqual(res.status_code, 200)
        self.q_old.refresh_from_db()
        self.assertEqual(self.q_old.moderation_status, ModerationStatus.REJECTED)
        self.assertEqual(self.q_old.moderation_note, "Too blurry")

    def test_acting_on_non_pending_conflicts(self):
        self.auth(self.staff)
        self.client.post(f"/api/moderation/questions/{self.q_old.id}/approve/")
        self.assertEqual(self.client.post(f"/api/moderation/questions/{self.q_old.id}/approve/").status_code, 409)
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{self.q_old.id}/reject/", {"note": "x"}).status_code, 409
        )

    def test_category_approve_does_not_touch_its_questions(self):
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/categories/{self.pending_cat.id}/approve/")
        self.assertEqual(res.status_code, 200)
        self.q_old.refresh_from_db()
        self.assertEqual(self.q_old.moderation_status, ModerationStatus.PENDING)

    def test_counts(self):
        self.auth(self.staff)
        res = self.client.get("/api/moderation/counts/")
        self.assertEqual(res.data, {"categories": 1, "questions": 2})

    def test_visibility_after_review_in_normal_lists(self):
        self.auth(self.staff)
        self.client.post(f"/api/moderation/questions/{self.q_old.id}/approve/")
        self.client.post(f"/api/moderation/questions/{self.q_new.id}/reject/", {"note": "Duplicate of an official one"})

        # A third party sees the approved question but not the rejected one.
        third = User.objects.create_user("third@test.com", "sturdy-pass-123")
        self.auth(third)
        res = self.client.get(f"/api/questions/?category={self.cat.id}")
        # (Category is still private, but questions are listed by their own visibility.)
        texts = [r["question_text"] for r in res.data["results"]]
        self.assertIn("Oldest?", texts)
        self.assertNotIn("Newest?", texts)

        # The owner still sees their rejected item, note included, on /create's data.
        self.auth(self.other)
        res = self.client.get("/api/questions/")
        mine = next(r for r in res.data["results"] if r["id"] == self.q_new.id)
        self.assertEqual(mine["moderation_status"], "rejected")
        self.assertEqual(mine["moderation_note"], "Duplicate of an official one")

    def test_profile_reports_is_staff_read_only(self):
        self.auth(self.staff)
        self.assertIs(self.client.get("/api/auth/profile/").data["is_staff"], True)
        self.auth(self.creator)
        self.assertIs(self.client.get("/api/auth/profile/").data["is_staff"], False)
        # And it can't be written.
        res = self.client.patch("/api/auth/profile/", {"is_staff": True})
        self.assertEqual(res.status_code, 200)
        self.creator.refresh_from_db()
        self.assertFalse(self.creator.is_staff)


# ---------------------------------------------------------------------------
# §G — bulk CSV upload
# ---------------------------------------------------------------------------

class BulkUploadTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.own_cat = make_category(self.creator, "Movies")
        self.official_cat = make_category(None, "Science", public_approved=True)

    def upload(self, user, text, **extra):
        self.auth(user)
        data = {"file": csv_file(text)}
        data.update(extra)
        return self.client.post("/api/questions/bulk/", data, format="multipart")

    def test_happy_path(self):
        res = self.upload(
            self.creator,
            HEADER
            + "Movies,Who directed Alien?,Ridley Scott,3,private\n"
            + "Movies,Best Picture 2020?,Parasite,2,public\n"
            + "Science,What is H2O?,Water,1,\n",  # blank visibility → private
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data, {
            "created": 3, "errors": [], "skipped": [], "dry_run": False,
            "categories_created": 0, "category_names": [],
        })
        mine = Question.objects.filter(owner=self.creator)
        self.assertEqual(mine.count(), 3)
        pub = mine.get(question_text="Best Picture 2020?")
        self.assertEqual(pub.visibility, Visibility.PUBLIC)
        self.assertEqual(pub.moderation_status, ModerationStatus.PENDING)  # straight into the §F queue
        blank = mine.get(question_text="What is H2O?")
        self.assertEqual(blank.visibility, Visibility.PRIVATE)
        self.assertEqual(blank.moderation_status, ModerationStatus.NOT_SUBMITTED)
        self.assertEqual(blank.media_type, "none")

    def test_all_or_nothing_with_1_based_rows(self):
        res = self.upload(
            self.creator,
            HEADER
            + "Movies,Fine question?,Yes,3,private\n"
            + "Movies,Bad difficulty?,Nope,nine,private\n",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["created"], 0)
        self.assertEqual(res.data["errors"], [
            {"row": 3, "field": "difficulty", "message": 'Difficulty must be a whole number 1–5 (got "nine").'},
        ])
        self.assertEqual(Question.objects.count(), 0)  # nothing created, not even row 2

    def test_multiple_row_errors_reported(self):
        res = self.upload(
            self.creator,
            HEADER
            + "Nowhere,Q?,A,1,private\n"
            + "Movies,,A,1,private\n"
            + "Movies,Q?,A,1,sometimes\n",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            [(e["row"], e["field"]) for e in res.data["errors"]],
            [(2, "category"), (3, "question_text"), (4, "visibility")],
        )

    def test_dry_run_creates_nothing(self):
        res = self.upload(self.creator, HEADER + "Movies,Q1?,A,1,private\n", dry_run="true")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {
            "created": 1, "errors": [], "skipped": [], "dry_run": True,
            "categories_created": 0, "category_names": [], "media_files": 0,
        })
        self.assertEqual(Question.objects.count(), 0)

    def test_skip_duplicates_on_reupload(self):
        text = HEADER + "Movies,Q1?,A,1,private\nMovies,Q2?,A,2,private\n"
        self.assertEqual(self.upload(self.creator, text).status_code, 201)
        res = self.upload(self.creator, text + "Movies,Q3?,A,3,private\n")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["created"], 1)
        self.assertEqual(res.data["skipped"], [2, 3])
        # In-file duplicate is also skipped.
        res = self.upload(self.creator, HEADER + "Movies,Q9?,A,1,private\nMovies,Q9?,A,1,private\n")
        self.assertEqual(res.data["created"], 1)
        self.assertEqual(res.data["skipped"], [3])

    @override_settings(PLAN_LIMITS=TINY_LIMITS)
    def test_batch_quota_boundary(self):
        make_question(self.creator, self.own_cat, "Existing")  # used = 1, limit = 3
        # 3 new rows would make 4 > 3 → structured 403 with requested.
        res = self.upload(
            self.creator,
            HEADER + "Movies,N1?,A,1,private\nMovies,N2?,A,1,private\nMovies,N3?,A,1,private\n",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_questions")
        self.assertEqual(res.data["used"], 1)
        self.assertEqual(res.data["limit"], 3)
        self.assertEqual(res.data["requested"], 3)
        self.assertEqual(Question.objects.filter(owner=self.creator).count(), 1)
        # Exactly filling the quota is allowed (used + count == limit).
        res = self.upload(self.creator, HEADER + "Movies,N1?,A,1,private\nMovies,N2?,A,1,private\n")
        self.assertEqual(res.status_code, 201, res.data)
        # Skipped duplicates don't count against the batch.
        res = self.upload(self.creator, HEADER + "Movies,N1?,A,1,private\n")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data, {
            "created": 0, "errors": [], "skipped": [2], "dry_run": False,
            "categories_created": 0, "category_names": [],
        })

    def test_free_user_gets_structured_quota_403_even_on_dry_run(self):
        res = self.upload(self.free, HEADER + "Science,Q?,A,1,private\n", dry_run="true")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_questions")
        self.assertEqual(res.data["limit"], 0)
        self.assertEqual(res.data["requested"], 1)

    def test_official_requires_staff(self):
        res = self.upload(self.creator, HEADER + "Science,Q?,A,1,private\n", official="true")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("code", res.data)  # plain detail, not a quota payload
        self.assertEqual(Question.objects.count(), 0)

    def test_official_upload_as_staff(self):
        # Staff account is on the free plan — official uploads are quota-exempt.
        res = self.upload(self.staff, HEADER + "Science,Q1?,A,1,private\nScience,Q2?,A,2,public\n", official="true")
        self.assertEqual(res.status_code, 201, res.data)
        rows = Question.objects.filter(categories=self.official_cat)
        self.assertEqual(rows.count(), 2)
        for q in rows:
            self.assertIsNone(q.owner)
            self.assertEqual(q.visibility, Visibility.PUBLIC)
            self.assertEqual(q.moderation_status, ModerationStatus.APPROVED)  # like seed_demo

    def test_official_matches_official_categories_only(self):
        res = self.upload(self.staff, HEADER + "Movies,Q?,A,1,private\n", official="true")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["errors"][0]["field"], "category")

    def test_category_eligibility_matches_single_form_rule(self):
        # `other`'s private category is not usable by `creator`…
        make_category(self.other, "Their Private")
        res = self.upload(self.creator, HEADER + "Their Private,Q?,A,1,private\n")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["errors"][0]["field"], "category")
        # …but their public-approved one is.
        make_category(self.other, "Their Public", public_approved=True)
        res = self.upload(self.creator, HEADER + "Their Public,Q?,A,1,private\n")
        self.assertEqual(res.status_code, 201, res.data)

    def test_file_level_errors_never_500(self):
        # Bad encoding.
        self.auth(self.creator)
        res = self.client.post(
            "/api/questions/bulk/",
            {"file": csv_file(b"\xff\xfe\x00bad bytes")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        # Missing header column.
        res = self.upload(self.creator, "category,question_text,answer\nMovies,Q?,A\n")
        self.assertEqual(res.status_code, 400)
        self.assertIn("difficulty", res.data["detail"])
        # Too many rows.
        big = HEADER + "".join(f"Movies,Q{i}?,A,1,private\n" for i in range(501))
        self.assertEqual(self.upload(self.creator, big).status_code, 400)
        # Oversize file (>1 MB).
        huge = HEADER + ("x" * (1024 * 1024 + 10))
        self.assertEqual(self.upload(self.creator, huge).status_code, 400)
        # No file at all.
        self.auth(self.creator)
        self.assertEqual(self.client.post("/api/questions/bulk/", {}, format="multipart").status_code, 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_bom_and_multiline_quoted_cells(self):
        text = "\ufeff" + HEADER + 'Movies,"Line one\nline two?",A,1,private\nMovies,After multiline?,A,2,private\n'
        res = self.upload(self.creator, text)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 2)
        self.assertTrue(Question.objects.filter(question_text="Line one\nline two?").exists())

    def test_row_numbers_count_records_not_physical_lines(self):
        # A multiline quoted cell in row 2; the bad row is record 3 even though
        # it sits on physical line 4.
        text = HEADER + 'Movies,"Two\nlines?",A,1,private\nMovies,Bad?,A,7,private\n'
        res = self.upload(self.creator, text)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["errors"][0]["row"], 3)

    def test_anonymous_rejected(self):
        self.client.force_authenticate(user=None)
        res = self.client.post("/api/questions/bulk/", {"file": csv_file(HEADER)}, format="multipart")
        self.assertEqual(res.status_code, 401)


# ---------------------------------------------------------------------------
# Handoff #5 §F — auto-create missing categories during bulk upload
# ---------------------------------------------------------------------------

TINY_CAT_LIMITS = {
    "free":    {"games_per_month": None, "categories": 0, "questions": 0},
    "creator": {"games_per_month": None, "categories": 1, "questions": 500},
}
TINY_BOTH_LIMITS = {
    "free":    {"games_per_month": None, "categories": 0, "questions": 0},
    "creator": {"games_per_month": None, "categories": 1, "questions": 1},
}


class AutoCreateCategoryTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.own_cat = make_category(self.creator, "Movies")
        self.official_cat = make_category(None, "Science", public_approved=True)

    def upload(self, user, text, **extra):
        self.auth(user)
        data = {"file": csv_file(text)}
        data.update(extra)
        return self.client.post("/api/questions/bulk/", data, format="multipart")

    def test_flag_off_unknown_category_still_row_error(self):
        # v1 regression: without the flag, nothing changed.
        res = self.upload(self.creator, HEADER + "Nowhere,Q?,A,1,private\n")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["errors"][0]["field"], "category")
        self.assertFalse(Category.objects.filter(name="Nowhere").exists())

    def test_flag_on_creates_private_categories_deduped_case_insensitively(self):
        res = self.upload(
            self.creator,
            HEADER
            + "New Cat,Q1?,A,1,private\n"
            + "new cat,Q2?,A,2,private\n"   # same category, different casing
            + "Movies,Q3?,A,1,private\n",   # resolves; no creation
            create_categories="true",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 3)
        self.assertEqual(res.data["categories_created"], 1)
        self.assertEqual(res.data["category_names"], ["New Cat"])  # first row's casing wins
        cat = Category.objects.get(name="New Cat")
        self.assertEqual(cat.owner, self.creator)
        self.assertEqual(cat.visibility, Visibility.PRIVATE)
        self.assertEqual(cat.moderation_status, ModerationStatus.NOT_SUBMITTED)  # not in the review queue
        self.assertEqual(cat.questions.count(), 2)
        self.assertEqual(Category.objects.filter(name__iexact="new cat").count(), 1)

    def test_dry_run_reports_would_create_and_writes_neither_model(self):
        res = self.upload(
            self.creator,
            HEADER + "Metal,Q1?,A,1,private\nMyths,Q2?,A,1,private\n",
            create_categories="true", dry_run="true",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["categories_created"], 2)
        self.assertEqual(res.data["category_names"], ["Metal", "Myths"])  # sorted
        self.assertEqual(res.data["media_files"], 0)
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(Category.objects.count(), 2)  # only the setUp fixtures

    def test_ambiguity_still_errors_even_with_flag(self):
        third = User.objects.create_user("third@test.com", "sturdy-pass-123", plan="creator")
        make_category(self.other, "Same", public_approved=True)
        make_category(third, "Same", public_approved=True)
        res = self.upload(self.creator, HEADER + "Same,Q?,A,1,private\n", create_categories="true")
        self.assertEqual(res.status_code, 400)
        self.assertIn("more than one", res.data["errors"][0]["message"])
        self.assertEqual(Category.objects.filter(name="Same").count(), 2)  # nothing new

    @override_settings(PLAN_LIMITS=TINY_CAT_LIMITS)
    def test_category_quota_boundary(self):
        # Movies already uses the whole quota of 1.
        res = self.upload(
            self.creator,
            HEADER + "Brand New,Q1?,A,1,private\n",
            create_categories="true",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_categories")
        self.assertEqual(res.data["used"], 1)
        self.assertEqual(res.data["limit"], 1)
        self.assertEqual(res.data["requested"], 1)
        self.assertFalse(Category.objects.filter(name="Brand New").exists())
        self.assertEqual(Question.objects.count(), 0)
        # Same denial on a dry run.
        res = self.upload(
            self.creator, HEADER + "Brand New,Q1?,A,1,private\n",
            create_categories="true", dry_run="true",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_categories")

    @override_settings(PLAN_LIMITS=TINY_BOTH_LIMITS)
    def test_category_denial_comes_before_question_denial(self):
        # Both quotas would be exceeded; the categories check runs first.
        res = self.upload(
            self.creator,
            HEADER + "A New,Q1?,A,1,private\nAnother,Q2?,A,1,private\n",
            create_categories="true",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_categories")
        self.assertEqual(res.data["requested"], 2)

    def test_free_user_with_flag_gets_quota_categories_403(self):
        res = self.upload(
            self.free, HEADER + "Nowhere,Q?,A,1,private\n",
            create_categories="true", dry_run="true",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "quota_categories")
        self.assertEqual(res.data["limit"], 0)
        self.assertEqual(res.data["requested"], 1)

    def test_official_create_categories_seed_demo_shape_and_quota_exempt(self):
        # Staff account is on the free plan — official creation is quota-exempt.
        res = self.upload(
            self.staff,
            HEADER + "Brand New,Q1?,A,1,private\nScience,Q2?,A,1,private\n",
            official="true", create_categories="true",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["categories_created"], 1)
        cat = Category.objects.get(name="Brand New")
        self.assertIsNone(cat.owner)
        self.assertEqual(cat.visibility, Visibility.PUBLIC)
        self.assertEqual(cat.moderation_status, ModerationStatus.APPROVED)
        q = cat.questions.get()
        self.assertIsNone(q.owner)

    def test_unique_constraint_race_is_400_not_500(self):
        # Concurrent uploads racing unique_category_name_per_owner are out of
        # scope beyond a clean 400 (§F1) — simulate the IntegrityError.
        with patch("trivia.views.create_new_categories", side_effect=IntegrityError("boom")):
            res = self.upload(
                self.creator, HEADER + "Racer,Q?,A,1,private\n", create_categories="true"
            )
        self.assertEqual(res.status_code, 400)
        self.assertIn("detail", res.data)
        self.assertEqual(Question.objects.count(), 0)
        self.assertFalse(Category.objects.filter(name="Racer").exists())


# ---------------------------------------------------------------------------
# Handoff #5 §G — zip-with-media bulk format
# ---------------------------------------------------------------------------

TMP_MEDIA = tempfile.mkdtemp(prefix="dq-test-media-")
MEDIA_HEADER = "category,question_text,answer,difficulty,visibility,media_type,media_file\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3_BYTES = b"ID3" + b"\x00" * 64


def zip_upload_file(csv_text, media=None, *, csv_name="questions.csv", name="upload.zip"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if csv_text is not None:
            zf.writestr(csv_name, csv_text)
        for path, data in (media or {}).items():
            zf.writestr(path, data)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="application/zip")


def stored_media_files():
    found = []
    for root, _, files in os.walk(TMP_MEDIA):
        found.extend(os.path.join(root, f) for f in files)
    return found


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ZipUploadTests(BaseCase):
    def setUp(self):
        super().setUp()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        os.makedirs(TMP_MEDIA, exist_ok=True)
        self.own_cat = make_category(self.creator, "Movies")
        self.official_cat = make_category(None, "Science", public_approved=True)

    def upload_zip(self, user, upload, **extra):
        self.auth(user)
        data = {"file": upload}
        data.update(extra)
        return self.client.post("/api/questions/bulk/", data, format="multipart")

    def test_zip_happy_path_with_image_and_audio(self):
        up = zip_upload_file(
            MEDIA_HEADER
            + "Movies,What film is this frame from?,Answer,2,private,image,media/frame.png\n"
            + "Movies,Name this soundtrack.,Answer,3,private,audio,clip.mp3\n"
            + "Movies,Plain text question?,Answer,1,private,,\n",
            media={"media/frame.png": PNG_BYTES, "clip.mp3": MP3_BYTES, "ignored/extra.txt": b"x"},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 3)
        img_q = Question.objects.get(question_text="What film is this frame from?")
        self.assertEqual(img_q.media_type, "image")
        self.assertTrue(img_q.image.name)
        self.assertTrue(img_q.image.storage.exists(img_q.image.name))
        self.assertEqual(img_q.image.read(), PNG_BYTES)
        audio_q = Question.objects.get(question_text="Name this soundtrack.")
        self.assertEqual(audio_q.media_type, "audio")
        self.assertTrue(audio_q.audio.storage.exists(audio_q.audio.name))
        plain_q = Question.objects.get(question_text="Plain text question?")
        self.assertEqual(plain_q.media_type, "none")
        self.assertFalse(plain_q.image)

    def test_dry_run_counts_media_and_saves_nothing(self):
        up = zip_upload_file(
            MEDIA_HEADER
            + "Movies,Image?,A,1,private,image,pic.png\n"
            + "Movies,Audio?,A,1,private,audio,clip.mp3\n"
            + "Movies,Plain?,A,1,private,,\n",
            media={"pic.png": PNG_BYTES, "clip.mp3": MP3_BYTES},
        )
        res = self.upload_zip(self.creator, up, dry_run="true")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["created"], 3)
        self.assertEqual(res.data["media_files"], 2)
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(stored_media_files(), [])  # no media landed in storage

    def test_missing_referenced_file_is_row_error_naming_path(self):
        up = zip_upload_file(MEDIA_HEADER + "Movies,Q?,A,1,private,image,media/gone.png\n")
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 400)
        err = res.data["errors"][0]
        self.assertEqual((err["row"], err["field"]), (2, "media_file"))
        self.assertIn("media/gone.png", err["message"])

    def test_media_column_mismatches_are_row_errors(self):
        up = zip_upload_file(
            MEDIA_HEADER
            + "Movies,Type no file?,A,1,private,image,\n"       # media_type set, file blank
            + "Movies,File no type?,A,1,private,,pic.png\n"     # file set, type blank
            + "Movies,Bad type?,A,1,private,hologram,pic.png\n",
            media={"pic.png": PNG_BYTES},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            [(e["row"], e["field"]) for e in res.data["errors"]],
            [(2, "media_file"), (3, "media_file"), (4, "media_type")],
        )
        self.assertEqual(Question.objects.count(), 0)

    def test_plain_csv_with_media_type_is_row_error_pointing_at_zip(self):
        self.auth(self.creator)
        res = self.client.post(
            "/api/questions/bulk/",
            {"file": csv_file(MEDIA_HEADER + "Movies,Q?,A,1,private,image,pic.png\n")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("zip", res.data["errors"][0]["message"])

    def test_zip_slip_names_are_clean_400(self):
        for evil in ("../evil.png", "/abs.png", "..\\win.png", "C:\\drive.png"):
            up = zip_upload_file(
                MEDIA_HEADER + "Movies,Q?,A,1,private,image,pic.png\n",
                media={evil: PNG_BYTES, "pic.png": PNG_BYTES},
            )
            res = self.upload_zip(self.creator, up)
            self.assertEqual(res.status_code, 400, evil)
            self.assertIn("Unsafe path", res.data["detail"])
        self.assertEqual(Question.objects.count(), 0)

    def test_csv_member_count_rules(self):
        # No CSV at all.
        res = self.upload_zip(self.creator, zip_upload_file(None, media={"pic.png": PNG_BYTES}))
        self.assertEqual(res.status_code, 400)
        # Two CSVs at the root.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.csv", MEDIA_HEADER)
            zf.writestr("b.csv", MEDIA_HEADER)
        res = self.upload_zip(
            self.creator, SimpleUploadedFile("two.zip", buf.getvalue(), content_type="application/zip")
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("exactly one CSV", res.data["detail"])
        # Only a nested CSV (user zipped the folder).
        res = self.upload_zip(
            self.creator,
            zip_upload_file(MEDIA_HEADER + "Movies,Q?,A,1,private,,\n", csv_name="folder/questions.csv"),
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("root", res.data["detail"])

    def test_validator_rejects_wrong_type_and_oversize_as_row_errors(self):
        # A text file claiming to be an image → validator row error, not a 500.
        up = zip_upload_file(
            MEDIA_HEADER + "Movies,Q?,A,1,private,image,notes.txt\n",
            media={"notes.txt": b"just text"},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported image type", res.data["errors"][0]["message"])
        # Uncompressed size over the image cap (§F2: 10 MB) → row error before
        # extraction.
        up = zip_upload_file(
            MEDIA_HEADER + "Movies,Q?,A,1,private,image,big.png\n",
            media={"big.png": b"\x00" * (11 * 1024 * 1024)},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 400)
        self.assertIn("too large", res.data["errors"][0]["message"])

    def test_all_or_nothing_leaves_no_orphaned_media(self):
        up = zip_upload_file(
            MEDIA_HEADER
            + "Movies,Good media row?,A,1,private,image,pic.png\n"
            + "Movies,Bad difficulty?,A,nine,private,,\n",
            media={"pic.png": PNG_BYTES},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["created"], 0)
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(stored_media_files(), [])  # no orphaned files

    def test_official_zip_keeps_media_on_owner_null_rows(self):
        up = zip_upload_file(
            MEDIA_HEADER + "Science,Official image?,A,1,private,image,pic.png\n",
            media={"pic.png": PNG_BYTES},
        )
        res = self.upload_zip(self.staff, up, official="true")
        self.assertEqual(res.status_code, 201, res.data)
        q = Question.objects.get(question_text="Official image?")
        self.assertIsNone(q.owner)
        self.assertEqual(q.visibility, Visibility.PUBLIC)
        self.assertEqual(q.moderation_status, ModerationStatus.APPROVED)
        self.assertEqual(q.media_type, "image")
        self.assertTrue(q.image.storage.exists(q.image.name))

    def test_zip_combined_with_create_categories(self):
        up = zip_upload_file(
            MEDIA_HEADER + "Fresh Cat,Image in a new category?,A,1,private,image,pic.png\n",
            media={"pic.png": PNG_BYTES},
        )
        res = self.upload_zip(self.creator, up, create_categories="true")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["categories_created"], 1)
        self.assertEqual(res.data["category_names"], ["Fresh Cat"])
        cat = Category.objects.get(name="Fresh Cat")
        self.assertEqual(cat.owner, self.creator)
        q = cat.questions.get()
        self.assertEqual(q.media_type, "image")
        self.assertTrue(q.image.storage.exists(q.image.name))

    def test_duplicate_media_rows_skipped_on_reupload(self):
        make_question(self.creator, self.own_cat, "Image?")  # pre-existing duplicate
        up = zip_upload_file(
            MEDIA_HEADER + "Movies,Image?,A,1,private,image,pic.png\n",
            media={"pic.png": PNG_BYTES},
        )
        res = self.upload_zip(self.creator, up)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 0)
        self.assertEqual(res.data["skipped"], [2])
        self.assertEqual(stored_media_files(), [])  # skipped rows save nothing


# ---------------------------------------------------------------------------
# Handoff #7 §F2 — per-file limits, decompression-bomb guard, image auto-resize
# ---------------------------------------------------------------------------

from PIL import Image  # noqa: E402  (backend dependency; tests build real fixtures)


def jpeg_bytes(width, height, *, orientation=None, color=(200, 60, 60), quality=95):
    """A real JPEG fixture. `orientation` writes the EXIF Orientation tag
    (6 = rotate 90° CW on display) so exif_transpose has work to do."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    kwargs = {"format": "JPEG", "quality": quality}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation  # Orientation
        exif[0x8825] = {1: b"N", 2: (37, 46, 12)}  # GPSInfo — must be stripped on output
        kwargs["exif"] = exif
    img.save(buf, **kwargs)
    return buf.getvalue()


def png_alpha_bytes(width, height):
    img = Image.new("RGBA", (width, height), (10, 200, 90, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def bomb_png_bytes(side=8000):
    """Huge pixels (64 MP > the 40 MP cap), tiny bytes — the classic bomb."""
    img = Image.new("L", (side, side), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def uploaded(name, data, content_type):
    return SimpleUploadedFile(name, data, content_type=content_type)


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class MediaProcessingTests(BaseCase):
    """§F2: identical behavior on the direct create path and the bulk zip path
    (one shared implementation in trivia/images.py, entered via model save)."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        os.makedirs(TMP_MEDIA, exist_ok=True)
        self.cat = make_category(self.creator, "Movies")

    def create_question(self, *, media_type, upload, field=None):
        self.auth(self.creator)
        field = field or media_type
        return self.client.post(
            "/api/questions/",
            {
                "categories": [self.cat.id],  # §K1 (#11): alias removed
                "question_text": f"{media_type} q?",
                "answer": "A",
                "difficulty": 1,
                "visibility": "private",
                "media_type": media_type,
                field: upload,
            },
            format="multipart",
        )

    # --- hard rejects, direct path (same validator surface as before) -----

    def test_oversize_image_rejected_direct(self):
        data = jpeg_bytes(100, 100) + b"\x00" * (11 * 1024 * 1024)  # valid JPEG, padded past 10 MB
        res = self.create_question(media_type="image", upload=uploaded("big.jpg", data, "image/jpeg"))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("too large", str(res.data["image"]))

    def test_oversize_audio_rejected_direct(self):
        data = b"ID3" + b"\x00" * (9 * 1024 * 1024)  # 8 MB cap
        res = self.create_question(media_type="audio", upload=uploaded("clip.mp3", data, "audio/mpeg"))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("too large", str(res.data["audio"]))

    def test_oversize_video_rejected_direct(self):
        data = b"\x00" * (26 * 1024 * 1024)  # 25 MB cap (down from 50)
        res = self.create_question(media_type="video", upload=uploaded("clip.mp4", data, "video/mp4"))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("too large", str(res.data["video"]))

    def test_decompression_bomb_rejected_direct_without_decode(self):
        data = bomb_png_bytes()
        self.assertLess(len(data), 1024 * 1024)  # tiny bytes...
        res = self.create_question(media_type="image", upload=uploaded("bomb.png", data, "image/png"))
        self.assertEqual(res.status_code, 400, res.data)  # ...clean reject, not an OOM
        self.assertIn("megapixels", str(res.data["image"]))

    # --- auto-resize, direct path ------------------------------------------

    def stored_image(self, res):
        q = Question.objects.get(pk=res.data["id"])
        with q.image.open("rb") as f:
            data = f.read()
        return q, data, Image.open(io.BytesIO(data))

    def test_large_photo_resized_reencoded_exif_stripped(self):
        raw = jpeg_bytes(2600, 1400, orientation=1)
        res = self.create_question(media_type="image", upload=uploaded("photo.jpg", raw, "image/jpeg"))
        self.assertEqual(res.status_code, 201, res.data)
        q, data, img = self.stored_image(res)
        self.assertLessEqual(max(img.size), 1920)
        self.assertEqual(img.size, (1920, round(1400 * 1920 / 2600)))  # aspect kept
        self.assertLess(len(data), len(raw))
        self.assertEqual(img.format, "JPEG")
        self.assertFalse(dict(img.getexif()))  # EXIF (incl. the GPS fixture) gone
        self.assertEqual(q.media_bytes, len(data))  # §F3: counted post-resize

    def test_exif_orientation_applied_before_resize(self):
        # 2400×1200 stored sideways with Orientation=6 → displays as 1200×2400
        # portrait; the pipeline must transpose FIRST, then fit within 1920.
        raw = jpeg_bytes(2400, 1200, orientation=6)
        res = self.create_question(media_type="image", upload=uploaded("rot.jpg", raw, "image/jpeg"))
        self.assertEqual(res.status_code, 201, res.data)
        _, _, img = self.stored_image(res)
        self.assertGreater(img.size[1], img.size[0])  # portrait after transpose
        self.assertEqual(img.size, (960, 1920))

    def test_png_with_alpha_stays_png(self):
        raw = png_alpha_bytes(2400, 2400)
        res = self.create_question(media_type="image", upload=uploaded("logo.png", raw, "image/png"))
        self.assertEqual(res.status_code, 201, res.data)
        q, _, img = self.stored_image(res)
        self.assertEqual(img.format, "PNG")
        self.assertIn(img.mode, ("RGBA", "LA"))
        self.assertLessEqual(max(img.size), 1920)
        self.assertTrue(q.image.name.endswith(".png"))

    def test_small_image_stored_byte_for_byte(self):
        raw = jpeg_bytes(800, 600)  # under 1 MB and under 1920 px → untouched
        res = self.create_question(media_type="image", upload=uploaded("small.jpg", raw, "image/jpeg"))
        self.assertEqual(res.status_code, 201, res.data)
        _, data, _ = self.stored_image(res)
        self.assertEqual(data, raw)

    # --- the SAME behavior through the bulk zip path -----------------------

    def bulk_zip(self, media, row="Movies,Zip image?,A,1,private,image,pic.img"):
        self.auth(self.creator)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("questions.csv", MEDIA_HEADER + row + "\n")
            for path, data in media.items():
                zf.writestr(path, data)
        up = SimpleUploadedFile("bulk.zip", buf.getvalue(), content_type="application/zip")
        return self.client.post("/api/questions/bulk/", {"file": up}, format="multipart")

    def test_bulk_zip_image_resized_by_same_implementation(self):
        raw = jpeg_bytes(2600, 1400)
        res = self.bulk_zip({"pic.jpg": raw}, row="Movies,Zip image?,A,1,private,image,pic.jpg")
        self.assertEqual(res.status_code, 201, res.data)
        q = Question.objects.get(question_text="Zip image?")
        with q.image.open("rb") as f:
            img = Image.open(io.BytesIO(f.read()))
        self.assertLessEqual(max(img.size), 1920)
        self.assertEqual(img.format, "JPEG")

    def test_bulk_zip_decompression_bomb_is_row_error(self):
        res = self.bulk_zip({"bomb.png": bomb_png_bytes()},
                            row="Movies,Boom?,A,1,private,image,bomb.png")
        self.assertEqual(res.status_code, 400, res.data)
        err = res.data["errors"][0]
        self.assertEqual((err["row"], err["field"]), (2, "media_file"))
        self.assertIn("megapixels", err["message"])
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(stored_media_files(), [])


# ---------------------------------------------------------------------------
# Handoff #7 §F3 — per-plan storage quota
# ---------------------------------------------------------------------------

STORAGE_LIMITS = {
    "free": {"games_per_month": None, "categories": 0, "questions": 0, "storage_bytes": 0},
    "creator": {"games_per_month": None, "categories": 25, "questions": 500,
                "storage_bytes": 200 * 1024},  # 200 KB — tiny, so small fixtures trip it
}


@override_settings(MEDIA_ROOT=TMP_MEDIA, PLAN_LIMITS=STORAGE_LIMITS)
class StorageQuotaTests(BaseCase):
    def setUp(self):
        super().setUp()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        os.makedirs(TMP_MEDIA, exist_ok=True)
        self.cat = make_category(self.creator, "Movies")

    def post_question(self, user, upload):
        self.auth(user)
        return self.client.post(
            "/api/questions/",
            {"categories": [self.cat.id], "question_text": f"Q{Question.objects.count()}?",  # §K1
             "answer": "A", "difficulty": 1, "visibility": "private",
             "media_type": "image", "image": upload},
            format="multipart",
        )

    def small_png(self, kb):
        # Incompressible noise so stored size ≈ requested size (and no resize:
        # tiny dims, under the 1 MB threshold).
        img = Image.frombytes("L", (256, kb * 4), os.urandom(256 * kb * 4))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return SimpleUploadedFile("noise.png", buf.getvalue(), content_type="image/png")

    def test_counted_from_persisted_sizes_and_freed_on_delete(self):
        from accounts.quotas import storage_bytes_used

        res = self.post_question(self.creator, self.small_png(30))
        self.assertEqual(res.status_code, 201, res.data)
        q = Question.objects.get(pk=res.data["id"])
        self.assertGreater(q.media_bytes, 0)
        self.assertEqual(q.media_bytes, q.image.size)
        self.assertEqual(storage_bytes_used(self.creator), q.media_bytes)
        # Profile usage block gains the storage entry (D3 shape).
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["usage"]["storage"], {"used": q.media_bytes, "limit": 200 * 1024})
        # Deleting the row frees the quota (the column goes with it).
        self.client.delete(f"/api/questions/{q.pk}/")
        self.assertEqual(storage_bytes_used(self.creator), 0)

    def test_single_file_denial_exact_shape(self):
        self.assertEqual(self.post_question(self.creator, self.small_png(120)).status_code, 201)
        res = self.post_question(self.creator, self.small_png(120))  # ~240 KB total > 200 KB
        self.assertEqual(res.status_code, 403, res.data)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code", "used", "limit"})  # documented contract, no extras
        self.assertEqual(body["code"], "quota_storage")
        self.assertEqual(body["limit"], 200 * 1024)
        self.assertGreater(body["used"], 0)
        self.assertIsInstance(body["used"], int)

    def test_patch_with_file_also_checked(self):
        res = self.post_question(self.creator, self.small_png(120))
        qid = res.data["id"]
        patch = self.client.patch(f"/api/questions/{qid}/", {"image": self.small_png(120)},
                                  format="multipart")
        self.assertEqual(patch.status_code, 403, patch.data)
        self.assertEqual(patch.json()["code"], "quota_storage")

    def test_bulk_batch_denial_carries_requested_bytes(self):
        img = self.small_png(150)  # one media row past the creator's 200 KB
        payload = img.read()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("questions.csv", MEDIA_HEADER + "Movies,Zip?,A,1,private,image,pic.png\n"
                        + "Movies,Zip2?,A,1,private,image,pic2.png\n")
            zf.writestr("pic.png", payload)
            zf.writestr("pic2.png", payload)
        self.auth(self.creator)
        res = self.client.post(
            "/api/questions/bulk/",
            {"file": SimpleUploadedFile("b.zip", buf.getvalue(), content_type="application/zip")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 403, res.data)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code", "used", "limit", "requested"})
        self.assertEqual(body["code"], "quota_storage")
        self.assertEqual(body["requested"], 2 * len(payload))  # total bytes of the batch
        self.assertEqual(Question.objects.count(), 0)

    def test_free_plan_storage_is_zero_and_free_cannot_upload_anyway(self):
        self.auth(self.free)
        p = self.client.get("/api/auth/profile/").json()
        self.assertEqual(p["usage"]["storage"], {"used": 0, "limit": 0})
        res = self.post_question(self.free, self.small_png(1))
        self.assertEqual(res.status_code, 403)  # question quota (0) fires first
        self.assertEqual(res.json()["code"], "quota_questions")

    def test_category_photo_counts_too(self):
        from accounts.quotas import storage_bytes_used

        self.auth(self.creator)
        res = self.client.post(
            "/api/categories/",
            {"name": "Snapped", "visibility": "private", "photo": self.small_png(40)},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        cat = Category.objects.get(name="Snapped")
        self.assertGreater(cat.photo_bytes, 0)
        self.assertEqual(storage_bytes_used(self.creator), cat.photo_bytes)


# ---------------------------------------------------------------------------
# Handoff #8 §K1 — near-duplicate ranking for reviewers
# ---------------------------------------------------------------------------
class SimilarQuestionsTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(None, "Movies", public_approved=True)
        self.dupe = Question.objects.create(
            owner=None,
            question_text="Which actor played the Joker in The Dark Knight?",
            answer="Heath Ledger", difficulty=2,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.dupe.categories.add(self.cat)
        self.unrelated = Question.objects.create(
            owner=None,
            question_text="What year did the first talkie premiere?",
            answer="1927", difficulty=3,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.unrelated.categories.add(self.cat)
        self.pending = make_question(
            self.creator, self.cat, "Who played the Joker in The Dark Knight?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )

    def test_planted_near_duplicate_ranks_first_with_usage_and_answer(self):
        self.client.force_authenticate(self.staff)
        res = self.client.get(f"/api/moderation/questions/{self.pending.id}/similar/")
        self.assertEqual(res.status_code, 200, res.data)
        similar = res.json()["similar"]
        self.assertGreaterEqual(len(similar), 1)
        top = similar[0]
        self.assertEqual(top["id"], self.dupe.id)
        self.assertEqual(top["answer"], "Heath Ledger")  # reviewers see the answer
        self.assertEqual(top["usage_count"], 0)  # §J1 count rides along
        self.assertGreater(top["score"], 0.5)
        # The pending question itself is never its own match.
        self.assertNotIn(self.pending.id, [row["id"] for row in similar])

    def test_similar_is_staff_only(self):
        self.client.force_authenticate(self.creator)
        self.assertEqual(
            self.client.get(f"/api/moderation/questions/{self.pending.id}/similar/").status_code, 403
        )

    def test_moderation_list_carries_usage_count(self):
        from games.models import BoardCell, BoardColumn, Game

        game = Game.objects.create(host=self.creator, mode="drinks")
        column = BoardColumn.objects.create(game=game, category=self.cat, position=0)
        BoardCell.objects.create(game=game, column=column, question=self.dupe, row=0, value=1)
        self.client.force_authenticate(self.staff)
        rows = self.client.get("/api/moderation/questions/?status=approved").json()["results"]
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[self.dupe.id]["usage_count"], 1)
        self.assertEqual(by_id[self.unrelated.id]["usage_count"], 0)


# ---------------------------------------------------------------------------
# Handoff #8 §K2 — host flags a public question; moderators work the inflow
# ---------------------------------------------------------------------------
class QuestionFlagTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(None, "History", public_approved=True)
        self.question = Question.objects.create(
            owner=self.other,
            question_text="Approved but suspicious?", answer="Yes", difficulty=1,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.question.categories.add(self.cat)

    def flag(self, user, reason=""):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/questions/{self.question.id}/report/", {"reason": reason}, format="json")

    def test_any_authenticated_host_can_flag_even_on_the_free_plan(self):
        res = self.flag(self.free, "The answer is just wrong.")
        self.assertEqual(res.status_code, 201, res.data)
        report = QuestionReport.objects.get(pk=res.json()["id"])
        self.assertEqual(report.status, ReportStatus.OPEN)
        self.assertEqual(report.reason, "The answer is just wrong.")

    def test_second_open_flag_by_same_reporter_is_409_pinned(self):
        self.assertEqual(self.flag(self.free).status_code, 201)
        res = self.flag(self.free)
        self.assertEqual(res.status_code, 409)
        self.assertIn("detail", res.json())
        # A different reporter still files fine — the limiter is per reporter.
        self.assertEqual(self.flag(self.creator).status_code, 201)

    def test_unauthenticated_cannot_flag(self):
        self.client.force_authenticate(None)
        res = self.client.post(f"/api/questions/{self.question.id}/report/", {}, format="json")
        self.assertEqual(res.status_code, 401)

    def test_flagging_never_changes_moderation_status_or_availability(self):
        self.flag(self.free)
        self.question.refresh_from_db()
        self.assertEqual(self.question.moderation_status, ModerationStatus.APPROVED)
        # Still in category listings...
        self.client.force_authenticate(self.creator)
        listed = self.client.get(f"/api/questions/?category={self.cat.id}").json()["results"]
        self.assertIn(self.question.id, [q["id"] for q in listed])
        # ...and still usable for game builds until a moderator acts.
        from games.services import usable_questions

        self.assertIn(self.question, usable_questions(self.cat, self.creator))

    def test_flagged_tab_is_staff_only_and_lists_context(self):
        self.flag(self.free, "reason one")
        self.flag(self.creator, "reason two")
        self.client.force_authenticate(self.creator)
        self.assertEqual(self.client.get("/api/moderation/flags/").status_code, 403)
        self.client.force_authenticate(self.staff)
        res = self.client.get("/api/moderation/flags/")
        self.assertEqual(res.status_code, 200)
        rows = res.json()["results"]
        self.assertEqual(len(rows), 1)  # one question, two reports
        row = rows[0]
        self.assertEqual(row["id"], self.question.id)
        self.assertEqual(row["report_count"], 2)
        self.assertEqual(len(row["reports"]), 2)
        self.assertIn("usage_count", row)  # §J1 context on the tab
        self.assertEqual(row["answer"], "Yes")  # reviewers see the answer

    def test_resolve_dismiss_keeps_the_question_and_closes_reports(self):
        self.flag(self.free)
        self.client.force_authenticate(self.staff)
        res = self.client.post(f"/api/moderation/flags/{self.question.id}/resolve/", {"action": "dismiss"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.question.refresh_from_db()
        self.assertEqual(self.question.moderation_status, ModerationStatus.APPROVED)
        self.assertFalse(self.question.reports.filter(status=ReportStatus.OPEN).exists())
        # Resolving again: nothing open → 409, mirroring the queue's double-act guard.
        res = self.client.post(f"/api/moderation/flags/{self.question.id}/resolve/", {"action": "dismiss"}, format="json")
        self.assertEqual(res.status_code, 409)
        # Once resolved, the same host may flag again if the problem persists.
        self.assertEqual(self.flag(self.free).status_code, 201)

    def test_resolve_reject_applies_the_queue_reject_field_semantics(self):
        self.flag(self.free)
        self.client.force_authenticate(self.staff)
        # Note required, exactly like the pending queue's reject.
        res = self.client.post(f"/api/moderation/flags/{self.question.id}/resolve/", {"action": "reject"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            f"/api/moderation/flags/{self.question.id}/resolve/",
            {"action": "reject", "note": "Answer is factually wrong."}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.question.refresh_from_db()
        self.assertEqual(self.question.moderation_status, ModerationStatus.REJECTED)
        self.assertEqual(self.question.moderation_note, "Answer is factually wrong.")
        self.assertFalse(self.question.reports.filter(status=ReportStatus.OPEN).exists())
        # Documented consequence: gone from public listings and future builds...
        self.client.force_authenticate(self.creator)
        listed = self.client.get(f"/api/questions/?category={self.cat.id}").json()["results"]
        self.assertNotIn(self.question.id, [q["id"] for q in listed])
        # ...while games already containing it keep their copy (PROTECT FK).
        from games.models import BoardCell, BoardColumn, Game

        game = Game.objects.create(host=self.creator, mode="drinks")
        column = BoardColumn.objects.create(game=game, category=self.cat, position=0)
        cell = BoardCell.objects.create(game=game, column=column, question=self.question, row=0, value=1)
        self.assertEqual(cell.question_id, self.question.id)

    def test_pending_queue_mechanics_untouched_by_a_flag(self):
        # A flag on an APPROVED question must not make it actionable in the
        # pending queue (the 409 double-act guard behaves exactly as before).
        self.flag(self.free)
        self.client.force_authenticate(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.question.id}/reject/", {"note": "x"}, format="json")
        self.assertEqual(res.status_code, 409)


# ---------------------------------------------------------------------------
# Handoff #9 §I — question lifecycle: soft delete, versioned edits, library
# ---------------------------------------------------------------------------

from django.utils import timezone as _tz  # noqa: E402

from games.models import BoardCell, BoardColumn, Game  # noqa: E402
from games.services import replace_cell_question, usable_questions  # noqa: E402
from trivia.similarity import similar_questions  # noqa: E402


class QuestionLifecycleBase(BaseCase):
    """Shared fixtures: one public-approved category with approved questions,
    and a helper that puts a question on a (played) board."""

    def setUp(self):
        super().setUp()
        self.cat = make_category(self.creator, "History", public_approved=True)
        self.q1 = make_question(
            self.creator, self.cat, "Who crossed the Rubicon?",
            status=ModerationStatus.APPROVED, visibility=Visibility.PUBLIC,
        )
        self.q2 = make_question(
            self.creator, self.cat, "Who crossed the Delaware?",
            status=ModerationStatus.APPROVED, visibility=Visibility.PUBLIC,
        )

    def play_in_game(self, question, host=None):
        game = Game.objects.create(host=host or self.creator, mode="points")
        column = BoardColumn.objects.create(game=game, category=question.categories.first(), position=0)
        cell = BoardCell.objects.create(game=game, column=column, question=question, row=0, value=100)
        return game, cell

    def soft_delete(self, question):
        question.deleted_at = _tz.now()
        question.save(update_fields=["deleted_at"])
        return question


class SoftDeleteAbsenceAuditTests(QuestionLifecycleBase):
    """§I1: one test per 'active questions' surface — a deleted question is
    absent from each, while its undeleted siblings are unaffected."""

    def setUp(self):
        super().setUp()
        self.soft_delete(self.q1)

    def test_absent_from_public_question_listing(self):
        self.auth(self.free)
        ids = {q["id"] for q in self.client.get("/api/questions/").data["results"]}
        self.assertNotIn(self.q1.id, ids)
        self.assertIn(self.q2.id, ids)

    def test_absent_from_owner_listing_and_detail_404s(self):
        self.auth(self.creator)
        ids = {q["id"] for q in self.client.get("/api/questions/").data["results"]}
        self.assertNotIn(self.q1.id, ids)
        self.assertEqual(self.client.get(f"/api/questions/{self.q1.id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/questions/{self.q1.id}/", {"answer": "X"}).status_code, 404)

    def test_absent_from_usable_question_count(self):
        self.assertEqual(self.cat.usable_question_count(self.creator), 1)
        self.assertEqual(self.cat.usable_question_count(None), 1)

    def test_absent_from_game_builds_and_replace_pool(self):
        self.assertEqual(
            set(usable_questions(self.cat, self.creator).values_list("id", flat=True)), {self.q2.id}
        )
        # §J3 replace draws through the same pool: with q2 on the board and
        # q1 deleted, there is nothing left to swap in.
        game, cell = self.play_in_game(self.q2)
        with self.assertRaises(Exception) as ctx:
            replace_cell_question(code=game.code, cell_id=cell.id, host=self.creator)
        self.assertIn("No other usable questions", str(ctx.exception))

    def test_absent_from_moderation_pending_queue_and_counts(self):
        pending = make_question(
            self.other, self.cat, "Pending then deleted?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        self.soft_delete(pending)
        self.auth(self.staff)
        ids = {q["id"] for q in self.client.get("/api/moderation/questions/").data["results"]}
        self.assertNotIn(pending.id, ids)
        self.assertEqual(self.client.get("/api/moderation/counts/").data["questions"], 0)

    def test_absent_from_flags_list(self):
        QuestionReport.objects.create(question=self.q1, reporter=self.other, reason="stale")
        self.auth(self.staff)
        ids = {q["id"] for q in self.client.get("/api/moderation/flags/").data["results"]}
        self.assertNotIn(self.q1.id, ids)

    def test_absent_from_similarity_pool(self):
        probe = make_question(
            self.other, self.cat, "Who crossed the Rubicon river?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        matches = {m["id"] for m in similar_questions(probe)}
        self.assertNotIn(self.q1.id, matches)

    def test_absent_from_bulk_duplicate_check(self):
        # Deleting a question then re-uploading the same text must create it,
        # not skip it as a duplicate.
        self.auth(self.creator)
        row = f"History,{self.q1.question_text},Caesar,1,private\n"
        res = self.client.post(
            "/api/questions/bulk/", {"file": csv_file(HEADER + row), "skip_duplicates": "true"}
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["created"], 1)
        self.assertEqual(res.data["skipped"], [])

    def test_absent_from_quota_counters(self):
        from accounts.quotas import questions_used, storage_bytes_used

        self.assertEqual(questions_used(self.creator), 1)  # q2 only
        Question.objects.filter(pk=self.q2.pk).update(media_bytes=1000)
        Question.objects.filter(pk=self.q1.pk).update(media_bytes=5000)
        self.assertEqual(storage_bytes_used(self.creator), 1000)


class SoftDeleteEndpointTests(QuestionLifecycleBase):
    def test_owner_delete_is_soft_and_survives_played_games(self):
        """The latent ProtectedError bug: deleting an ever-played question
        used to 500. Now it's a 204 soft delete and the game report still
        shows the question's text."""
        game, cell = self.play_in_game(self.q1)
        game.status = "finished"
        game.finished_at = _tz.now()
        game.save(update_fields=["status", "finished_at"])
        self.auth(self.creator)
        res = self.client.delete(f"/api/questions/{self.q1.id}/")
        self.assertEqual(res.status_code, 204, res.content)
        self.q1.refresh_from_db()  # row intact, flagged deleted
        self.assertIsNotNone(self.q1.deleted_at)
        report = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(report.status_code, 200, report.content)
        texts = [q["question_text"] for col in report.data["columns"] for q in col["questions"]]
        self.assertIn("Who crossed the Rubicon?", texts)

    def test_staff_delete_any_owners_question_and_409_on_repeat(self):
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.q1.id}/delete/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNotNone(res.data["deleted_at"])
        self.assertEqual(self.client.post(f"/api/moderation/questions/{self.q1.id}/delete/").status_code, 409)

    def test_staff_delete_resolves_open_flags(self):
        QuestionReport.objects.create(question=self.q1, reporter=self.other, reason="bad")
        self.auth(self.staff)
        self.client.post(f"/api/moderation/questions/{self.q1.id}/delete/")
        self.assertFalse(self.q1.reports.filter(status=ReportStatus.OPEN).exists())
        self.assertTrue(self.q1.reports.filter(status=ReportStatus.RESOLVED).exists())

    def test_new_endpoints_are_staff_only(self):
        for user in (self.creator, self.free):
            self.auth(user)
            self.assertEqual(self.client.post(f"/api/moderation/questions/{self.q1.id}/delete/").status_code, 403)
            self.assertEqual(
                self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"answer": "X"}).status_code,
                403,
            )

    def test_review_actions_409_on_deleted_question(self):
        pending = make_question(
            self.other, self.cat, "Deleted mid-review?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        self.soft_delete(pending)
        self.auth(self.staff)
        self.assertEqual(self.client.post(f"/api/moderation/questions/{pending.id}/approve/").status_code, 409)
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{pending.id}/reject/", {"note": "x"}).status_code, 409
        )


class ReviseEndpointTests(QuestionLifecycleBase):
    def test_revise_lineage(self):
        """Old row soft-deleted with replaced_by set; new row approved, same
        owner/category, edits applied, untouched fields copied; open flags on
        the old row resolved."""
        QuestionReport.objects.create(question=self.q1, reporter=self.other, reason="typo")
        self.auth(self.staff)
        res = self.client.post(
            f"/api/moderation/questions/{self.q1.id}/revise/",
            {"question_text": "Who crossed the Rubicon in 49 BC?", "difficulty": 3},
        )
        self.assertEqual(res.status_code, 201, res.content)
        new_id = res.data["id"]
        self.assertNotEqual(new_id, self.q1.id)
        new = Question.objects.get(pk=new_id)
        self.q1.refresh_from_db()
        self.assertIsNotNone(self.q1.deleted_at)
        self.assertEqual(self.q1.replaced_by_id, new.id)
        self.assertEqual(new.question_text, "Who crossed the Rubicon in 49 BC?")
        self.assertEqual(new.difficulty, 3)
        self.assertEqual(new.answer, self.q1.answer)  # untouched field copied
        self.assertEqual(new.owner_id, self.creator.id)
        # §F2 (Handoff #10): the revision copies the SAME category set.
        self.assertEqual(list(new.categories.values_list("id", flat=True)), [self.cat.id])
        self.assertEqual(new.moderation_status, ModerationStatus.APPROVED)
        self.assertIsNone(new.deleted_at)
        self.assertFalse(self.q1.reports.filter(status=ReportStatus.OPEN).exists())

    def test_revise_keeps_media_by_reference(self):
        Question.objects.filter(pk=self.q1.pk).update(image="questions/images/shared.jpg", media_type="image")
        self.q1.refresh_from_db()
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"answer": "Julius Caesar"})
        self.assertEqual(res.status_code, 201, res.content)
        new = Question.objects.get(pk=res.data["id"])
        self.assertEqual(new.image.name, "questions/images/shared.jpg")  # same file, two rows

    def test_played_games_keep_the_old_text(self):
        game, cell = self.play_in_game(self.q1)
        self.auth(self.staff)
        res = self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"question_text": "New text?"})
        self.assertEqual(res.status_code, 201)
        cell.refresh_from_db()
        self.assertEqual(cell.question_id, self.q1.id)  # cell still points at the OLD row
        self.assertEqual(cell.question.question_text, "Who crossed the Rubicon?")

    def test_revise_validation_and_conflicts(self):
        self.auth(self.staff)
        # 400s: empty payload, blank text, out-of-range difficulty, bad visibility
        self.assertEqual(self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {}).status_code, 400)
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"question_text": "  "}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"difficulty": 9}).status_code, 400
        )
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"visibility": "secret"}).status_code,
            400,
        )
        # 409 on already-deleted (a second revise of the same row)
        self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"answer": "Caesar"})
        self.assertEqual(
            self.client.post(f"/api/moderation/questions/{self.q1.id}/revise/", {"answer": "Nope"}).status_code, 409
        )


class LibraryApiTests(QuestionLifecycleBase):
    """§I4: search / filters / deleted view / ordering / real pagination."""

    def setUp(self):
        super().setUp()
        self.official_cat = make_category(None, "Official Cat", public_approved=True)
        self.official_q = Question.objects.create(
            owner=None, question_text="Official filler?",
            answer="Yes", difficulty=1, visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED,
        )
        self.official_q.categories.add(self.official_cat)
        self.deleted_q = self.soft_delete(
            make_question(self.creator, self.cat, "Gone soon?", status=ModerationStatus.APPROVED,
                          visibility=Visibility.PUBLIC)
        )
        self.auth(self.staff)

    def lib(self, params):
        res = self.client.get(f"/api/moderation/questions/?status=all&{params}")
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def test_search_matches_text_or_answer(self):
        data = self.lib("search=rubicon")
        self.assertEqual({r["id"] for r in data["results"]}, {self.q1.id})
        data = self.lib("search=yes")  # the official row's ANSWER
        self.assertIn(self.official_q.id, {r["id"] for r in data["results"]})

    def test_owner_filter_and_official(self):
        data = self.lib("owner=creator@")
        self.assertEqual({r["owner_email"] for r in data["results"]}, {"creator@test.com"})
        data = self.lib("owner=official")
        self.assertEqual({r["id"] for r in data["results"]}, {self.official_q.id})

    def test_category_and_status_filters(self):
        data = self.lib(f"category={self.official_cat.id}")
        self.assertEqual({r["id"] for r in data["results"]}, {self.official_q.id})
        res = self.client.get("/api/moderation/questions/?status=approved")
        ids = {r["id"] for r in res.data["results"]}
        self.assertIn(self.q1.id, ids)
        self.assertNotIn(self.deleted_q.id, ids)  # deleted default = active

    def test_deleted_only_and_all(self):
        data = self.lib("deleted=only")
        self.assertEqual({r["id"] for r in data["results"]}, {self.deleted_q.id})
        self.assertIsNotNone(data["results"][0]["deleted_at"])
        self.assertIn("replaced_by", data["results"][0])
        all_ids = {r["id"] for r in self.lib("deleted=all")["results"]}
        self.assertIn(self.deleted_q.id, all_ids)
        self.assertIn(self.q1.id, all_ids)
        self.assertEqual(self.client.get("/api/moderation/questions/?deleted=bogus").status_code, 400)

    def test_ordering_whitelist(self):
        self.play_in_game(self.q2)  # q2 gets usage_count 1
        data = self.lib("ordering=-usage_count")
        self.assertEqual(data["results"][0]["id"], self.q2.id)
        self.assertEqual(self.client.get("/api/moderation/questions/?ordering=answer").status_code, 400)

    def test_pagination_shape_and_page_size(self):
        for i in range(30):
            make_question(self.creator, self.cat, f"Filler {i}?", status=ModerationStatus.APPROVED,
                          visibility=Visibility.PUBLIC)
        data = self.lib("")
        self.assertEqual(set(data) >= {"results", "count", "next", "previous"}, True)
        self.assertEqual(len(data["results"]), 25)  # explicit library page size
        self.assertIsNotNone(data["next"])

    def test_pending_queue_default_unchanged(self):
        """The queue tabs keep their exact defaults: pending-only, active-only,
        oldest first — no new parameter leaks into the default view."""
        pending = make_question(self.other, self.cat, "Still queued?",
                                status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC)
        res = self.client.get("/api/moderation/questions/")
        self.assertEqual([r["id"] for r in res.data["results"]], [pending.id])


# ---------------------------------------------------------------------------
# Handoff #9 §K3 — moderation outcome emails
# ---------------------------------------------------------------------------

from django.core import mail  # noqa: E402


class ModerationEmailTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(self.creator, "Music", public_approved=True)
        self.q = make_question(self.creator, self.cat, "Who wrote Hey Jude?",
                               status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC)
        self.auth(self.staff)

    def test_approve_emails_the_owner(self):
        res = self.client.post(f"/api/moderation/questions/{self.q.id}/approve/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["creator@test.com"])
        self.assertIn("approved", msg.subject)
        self.assertIn("Who wrote Hey Jude?", msg.body)
        self.assertIn("Music", msg.body)

    def test_reject_emails_the_owner_with_the_note(self):
        res = self.client.post(f"/api/moderation/questions/{self.q.id}/reject/", {"note": "Too easy"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Too easy", mail.outbox[0].body)

    def test_category_approval_emails_too(self):
        pending_cat = Category.objects.create(
            owner=self.creator, name="Pending Cat", visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.PENDING,
        )
        self.client.post(f"/api/moderation/categories/{pending_cat.id}/approve/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("category", mail.outbox[0].subject)
        self.assertIn("Pending Cat", mail.outbox[0].body)

    def test_official_content_sends_nothing(self):
        official = make_question(None, self.cat, "Official pending?",
                                 status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC)
        self.client.post(f"/api/moderation/questions/{official.id}/approve/")
        self.assertEqual(len(mail.outbox), 0)

    def test_self_approval_sends_nothing(self):
        own = make_question(self.staff, self.cat, "Staff's own?",
                            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC)
        self.client.post(f"/api/moderation/questions/{own.id}/approve/")
        self.assertEqual(len(mail.outbox), 0)

    def test_flag_resolve_reject_emails_through_the_same_helper(self):
        self.q.moderation_status = ModerationStatus.APPROVED
        self.q.save(update_fields=["moderation_status"])
        QuestionReport.objects.create(question=self.q, reporter=self.other, reason="wrong answer")
        res = self.client.post(f"/api/moderation/flags/{self.q.id}/resolve/",
                               {"action": "reject", "note": "Answer is wrong"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Answer is wrong", mail.outbox[0].body)

    def test_email_failure_never_breaks_the_action(self):
        with patch("accounts.emails.send_mail", side_effect=RuntimeError("ESP down")):
            res = self.client.post(f"/api/moderation/questions/{self.q.id}/approve/")
        self.assertEqual(res.status_code, 200, res.content)
        self.q.refresh_from_db()
        self.assertEqual(self.q.moderation_status, ModerationStatus.APPROVED)


# ---------------------------------------------------------------------------
# Handoff #10 §F — questions belong to multiple categories
# ---------------------------------------------------------------------------

class MultiCategoryApiTests(BaseCase):
    """§F2: the write path — `categories` (list), the deprecated `category`
    back-compat alias, and the per-category permission rule."""

    def setUp(self):
        super().setUp()
        self.own = make_category(self.creator, "Mine")
        self.official = make_category(None, "Official", public_approved=True)
        self.foreign_private = make_category(self.other, "Theirs (private)")
        self.auth(self.creator)

    def post_question(self, **overrides):
        body = {"question_text": "Multi?", "answer": "Yes", "difficulty": 2, "visibility": "private"}
        body.update(overrides)
        return self.client.post("/api/questions/", body)

    def test_create_with_two_categories(self):
        res = self.post_question(categories=[self.own.id, self.official.id])
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(sorted(res.data["categories"]), sorted([self.own.id, self.official.id]))
        # The deprecated write alias never appears in responses.
        self.assertNotIn("category", res.data)
        q = Question.objects.get(pk=res.data["id"])
        self.assertEqual(q.categories.count(), 2)

    def test_legacy_single_category_write_is_a_clean_400(self):
        # §K1 (Handoff #11): the deprecation is COMPLETE — #10's one-session
        # write alias is gone. A `category`-only body is now just a body with
        # no `categories`, and the 400 names the field the client should send.
        res = self.post_question(category=self.own.id)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("categories", res.data)
        self.assertEqual(Question.objects.count(), 0)

    def test_at_least_one_category_is_required(self):
        res = self.post_question(categories=[])
        self.assertEqual(res.status_code, 400)
        self.assertIn("categories", res.data)

    def test_one_ineligible_category_fails_the_whole_create(self):
        res = self.post_question(categories=[self.own.id, self.foreign_private.id])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_patch_without_categories_keeps_the_set(self):
        q = make_question(self.creator, [self.own, self.official], "Keep cats?")
        res = self.client.patch(f"/api/questions/{q.id}/", {"answer": "New answer"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(q.categories.count(), 2)

    def test_patch_with_categories_replaces_the_set(self):
        q = make_question(self.creator, [self.own], "Move cats?")
        res = self.client.patch(f"/api/questions/{q.id}/", {"categories": [self.official.id]})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(list(q.categories.values_list("id", flat=True)), [self.official.id])

    def test_duplicate_ids_in_the_list_collapse(self):
        res = self.post_question(categories=[self.own.id, self.own.id])
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["categories"], [self.own.id])

    def test_question_list_filter_through_m2m_is_duplicate_free(self):
        q = make_question(self.creator, [self.own, self.official], "Once only?")
        listed = self.client.get("/api/questions/").json()["results"]
        self.assertEqual([row["id"] for row in listed].count(q.id), 1)
        by_cat = self.client.get(f"/api/questions/?category={self.own.id}").json()["results"]
        self.assertIn(q.id, [row["id"] for row in by_cat])


class BulkPipeCategoryTests(BaseCase):
    """§F2 CSV: pipe-separated category names + the new dedupe key."""

    HEADER = "category,question_text,answer,difficulty,visibility\n"

    def setUp(self):
        super().setUp()
        self.movies = make_category(None, "Movies", public_approved=True)
        self.music = make_category(None, "Music", public_approved=True)

    def upload(self, user, text, **data):
        self.auth(user)
        payload = {"file": csv_file(text)}
        payload.update(data)
        return self.client.post("/api/questions/bulk/", payload, format="multipart")

    def test_pipe_row_creates_one_question_in_two_categories(self):
        res = self.upload(self.creator, self.HEADER + "Movies|Music,Theme song?,Titanic,2,private\n")
        self.assertEqual(res.status_code, 201, res.data)
        q = Question.objects.get(question_text="Theme song?")
        self.assertEqual(
            sorted(q.categories.values_list("name", flat=True)), ["Movies", "Music"]
        )

    def test_pipe_with_auto_create_resolves_per_name(self):
        res = self.upload(
            self.creator, self.HEADER + "Movies|Brand New,Mixed?,A,1,private\n", create_categories="true"
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["category_names"], ["Brand New"])  # only the new one created
        q = Question.objects.get(question_text="Mixed?")
        self.assertEqual(sorted(q.categories.values_list("name", flat=True)), ["Brand New", "Movies"])

    def test_one_bad_name_errors_the_whole_row(self):
        res = self.upload(self.creator, self.HEADER + "Movies|No Such Cat,Broken?,A,1,private\n")
        self.assertEqual(res.status_code, 400)
        errors = res.data["errors"]
        self.assertEqual(errors[0]["field"], "category")
        self.assertIn("No Such Cat", errors[0]["message"])
        self.assertEqual(Question.objects.count(), 0)

    def test_dedupe_key_is_text_only_now(self):
        # BEHAVIOR CHANGE (decided, §F2): same text in a DIFFERENT category
        # is a duplicate — the M2M made "one question in both" the way to
        # express that, so the upload skips it.
        existing = make_question(self.creator, [self.movies], "Same text?")
        res = self.upload(self.creator, self.HEADER + "Music,Same text?,A,1,private\n")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 0)
        self.assertEqual(res.data["skipped"], [2])
        # The existing question was NOT silently recategorized either.
        self.assertEqual(list(existing.categories.values_list("id", flat=True)), [self.movies.id])

    def test_same_text_twice_in_one_file_dedupes_across_categories(self):
        res = self.upload(
            self.creator,
            self.HEADER + "Movies,Twice?,A,1,private\nMusic,Twice?,A,1,private\n",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 1)
        self.assertEqual(res.data["skipped"], [3])

    def test_repeated_name_within_one_row_collapses(self):
        res = self.upload(self.creator, self.HEADER + "Movies|movies,Twins?,A,1,private\n")
        self.assertEqual(res.status_code, 201, res.data)
        q = Question.objects.get(question_text="Twins?")
        self.assertEqual(list(q.categories.values_list("name", flat=True)), ["Movies"])


class SimilaritySharesAnyCategoryTests(BaseCase):
    """§F4: the primary similarity pool is 'shares ANY category'."""

    def test_candidate_sharing_one_of_two_categories_is_in_the_pool(self):
        movies = make_category(None, "Movies", public_approved=True)
        music = make_category(None, "Music", public_approved=True)
        target = make_question(
            self.creator, [movies, music], "Which band scored the film Flash Gordon?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        candidate = make_question(
            self.other, [music], "Which band scored the movie Flash Gordon soundtrack?",
            status=ModerationStatus.APPROVED, visibility=Visibility.PUBLIC,
        )
        from unittest.mock import patch as _patch

        from .similarity import similar_questions

        with _patch("trivia.similarity.MIN_CATEGORY_POOL", 1):
            matches = similar_questions(target)
        self.assertEqual(matches[0]["id"], candidate.id)
        # §F4: the per-match category context is a sorted NAME LIST now.
        self.assertEqual(matches[0]["category_names"], ["Music"])


# ---------------------------------------------------------------------------
# Handoff #10 §F5 — category soft delete (the absence audit)
# ---------------------------------------------------------------------------

class CategorySoftDeleteTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(self.creator, "Doomed")
        self.q = make_question(self.creator, [self.cat], "Orphan soon?")
        self.auth(self.creator)

    def delete_cat(self, cat=None):
        return self.client.delete(f"/api/categories/{(cat or self.cat).id}/")

    def test_owner_delete_is_soft(self):
        res = self.delete_cat()
        self.assertEqual(res.status_code, 204)
        self.cat.refresh_from_db()
        self.assertIsNotNone(self.cat.deleted_at)  # row remains

    def test_deleting_a_played_category_succeeds_and_history_survives(self):
        # The latent ProtectedError-500: BoardColumn.category is PROTECT.
        from games.services import create_game, finalize_game

        for i in range(2):
            make_question(self.creator, [self.cat], f"Filler {i}?")
        game = create_game(host=self.creator, mode="points", category_ids=[self.cat.id], questions_per_category=2)
        finalize_game(code=game.code)
        self.assertEqual(self.delete_cat().status_code, 204)
        # History/report intact: the column still names the category.
        res = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.json()["columns"][0]["category_name"], "Doomed")
        # And the public snapshot still serializes it (deleted rows REMAIN in
        # board columns — history is history).
        snap = self.client.get(f"/api/games/{game.code}/").json()
        self.assertEqual(snap["columns"][0]["category_name"], "Doomed")

    def test_absent_from_listings_and_404_on_detail(self):
        self.delete_cat()
        listed = self.client.get("/api/categories/").json()["results"]
        self.assertNotIn(self.cat.id, [c["id"] for c in listed])
        self.assertEqual(self.client.get(f"/api/categories/{self.cat.id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/categories/{self.cat.id}/", {"name": "Back"}).status_code, 404)

    def test_name_reuse_after_delete(self):
        self.delete_cat()
        res = self.client.post("/api/categories/", {"name": "Doomed", "visibility": "private"})
        self.assertEqual(res.status_code, 201, res.data)  # partial constraint permits it

    def test_bulk_upload_cannot_resolve_a_deleted_category(self):
        self.delete_cat()
        res = self.client.post(
            "/api/questions/bulk/",
            {"file": csv_file("category,question_text,answer,difficulty,visibility\nDoomed,Q?,A,1,private\n")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["errors"][0]["field"], "category")

    def test_bulk_auto_create_makes_a_fresh_category_despite_the_deleted_name(self):
        self.delete_cat()
        res = self.client.post(
            "/api/questions/bulk/",
            {
                "file": csv_file("category,question_text,answer,difficulty,visibility\nDoomed,Fresh?,A,1,private\n"),
                "create_categories": "true",
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        fresh = Question.objects.get(question_text="Fresh?").categories.get()
        self.assertNotEqual(fresh.id, self.cat.id)  # NOT the deleted row
        self.assertIsNone(fresh.deleted_at)

    def test_question_creation_cannot_target_a_deleted_category(self):
        self.delete_cat()
        res = self.client.post(
            "/api/questions/",
            {"categories": [self.cat.id], "question_text": "Into the void?", "answer": "No", "difficulty": 1},
        )
        self.assertEqual(res.status_code, 400)

    def test_moderation_queue_and_counts_exclude_deleted_categories(self):
        self.cat.submit_for_review()
        self.cat.save()
        self.auth(self.staff)
        before = self.client.get("/api/moderation/counts/").json()["categories"]
        self.assertGreaterEqual(before, 1)
        self.auth(self.creator)
        self.delete_cat()
        self.auth(self.staff)
        counts = self.client.get("/api/moderation/counts/").json()
        self.assertEqual(counts["categories"], before - 1)
        queue = self.client.get("/api/moderation/categories/").json()["results"]
        self.assertNotIn(self.cat.id, [c["id"] for c in queue])
        # Reviewing the deleted row races cleanly: the shared 409, not a 404.
        res = self.client.post(f"/api/moderation/categories/{self.cat.id}/approve/")
        self.assertEqual(res.status_code, 409)

    def test_quota_counters_exclude_deleted_categories(self):
        from accounts.quotas import categories_used, storage_bytes_used

        self.cat.photo_bytes = 1000
        self.cat.save(update_fields=["photo_bytes"])
        self.assertEqual(categories_used(self.creator), 1)
        used_before = storage_bytes_used(self.creator)
        self.delete_cat()
        self.assertEqual(categories_used(self.creator), 0)
        self.assertEqual(storage_bytes_used(self.creator), used_before - 1000)

    def test_questions_are_not_cascaded_and_stay_in_the_owners_list(self):
        self.delete_cat()
        self.q.refresh_from_db()
        self.assertIsNone(self.q.deleted_at)  # NOT deleted with the category
        listed = self.client.get("/api/questions/").json()["results"]
        self.assertIn(self.q.id, [row["id"] for row in listed])
        # ...and it can be recategorized via a normal PATCH.
        fresh = make_category(self.creator, "New Home")
        res = self.client.patch(f"/api/questions/{self.q.id}/", {"categories": [fresh.id]})
        self.assertEqual(res.status_code, 200, res.data)

    def test_orphaned_question_is_invisible_to_game_builds(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from games.services import create_game

        self.delete_cat()
        # The deleted category id reads as unknown — a build can't reach the
        # orphan through it (usable_questions goes through a live category).
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(host=self.creator, mode="points", category_ids=[self.cat.id], questions_per_category=1)
        self.assertIn("Unknown category ids", str(ctx.exception.detail["categories"]))


# ---------------------------------------------------------------------------
# Handoff #10 §G — themes
# ---------------------------------------------------------------------------

class ThemeApiTests(BaseCase):
    def setUp(self):
        super().setUp()
        from .models import Theme

        self.movies = make_category(None, "Movies", public_approved=True)
        self.music = make_category(None, "Music", public_approved=True)
        for i in range(2):
            make_question(None, [self.movies], f"M{i}?", status=ModerationStatus.APPROVED,
                          visibility=Visibility.PUBLIC)
        self.private_cat = make_category(self.creator, "Creator Private")
        make_question(self.creator, [self.private_cat], "Private Q?")
        self.theme = Theme.objects.create(name="Screens", description="Film + sound")
        self.theme.categories.set([self.movies, self.music, self.private_cat])

    def test_list_shape_and_per_user_visibility_and_counts(self):
        self.auth(self.creator)
        themes = self.client.get("/api/themes/").json()
        self.assertEqual(len(themes), 1)
        theme = themes[0]
        self.assertEqual(set(theme), {"id", "name", "description", "categories"})
        by_name = {c["name"]: c for c in theme["categories"]}
        # The creator sees their own private category, with THEIR count.
        self.assertEqual(set(by_name), {"Movies", "Music", "Creator Private"})
        self.assertEqual(by_name["Movies"]["usable_question_count"], 2)
        self.assertEqual(by_name["Creator Private"]["usable_question_count"], 1)
        # A different user does NOT see the creator's private category.
        self.auth(self.other)
        theme = self.client.get("/api/themes/").json()[0]
        self.assertEqual({c["name"] for c in theme["categories"]}, {"Movies", "Music"})

    def test_anon_gets_401_and_deleted_rows_are_excluded(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/themes/").status_code, 401)
        from django.utils import timezone as _tz

        self.movies.deleted_at = _tz.now()
        self.movies.save(update_fields=["deleted_at"])
        self.auth(self.creator)
        theme = self.client.get("/api/themes/").json()[0]
        self.assertNotIn("Movies", [c["name"] for c in theme["categories"]])  # §F5 reach
        self.theme.deleted_at = _tz.now()
        self.theme.save(update_fields=["deleted_at"])
        self.assertEqual(self.client.get("/api/themes/").json(), [])

    def test_staff_crud_round_trip(self):
        self.auth(self.staff)
        res = self.client.post(
            "/api/moderation/themes/",
            {"name": "Party", "description": "Bangers", "categories": [self.music.id]},
        )
        self.assertEqual(res.status_code, 201, res.data)
        theme_id = res.data["id"]
        self.assertEqual(res.data["category_names"], ["Music"])
        res = self.client.patch(
            f"/api/moderation/themes/{theme_id}/", {"categories": [self.music.id, self.movies.id]}
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["category_names"], ["Movies", "Music"])
        # Duplicate ACTIVE name → 400.
        res = self.client.post("/api/moderation/themes/", {"name": "Party"})
        self.assertEqual(res.status_code, 400)
        # Soft delete → 204; double delete → house 409; name is reusable.
        self.assertEqual(self.client.delete(f"/api/moderation/themes/{theme_id}/").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/moderation/themes/{theme_id}/").status_code, 409)
        listed = self.client.get("/api/moderation/themes/").json()
        self.assertNotIn(theme_id, [t["id"] for t in listed])
        res = self.client.post("/api/moderation/themes/", {"name": "Party"})
        self.assertEqual(res.status_code, 201, res.data)

    def test_staff_endpoints_reject_non_staff_and_anon(self):
        self.auth(self.creator)
        self.assertEqual(self.client.get("/api/moderation/themes/").status_code, 403)
        self.assertEqual(self.client.post("/api/moderation/themes/", {"name": "Nope"}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/moderation/themes/").status_code, 401)

    def test_seed_demo_is_idempotent_for_themes(self):
        from django.core.management import call_command

        from .models import Theme

        call_command("seed_demo")
        first = Theme.objects.filter(deleted_at__isnull=True).count()
        call_command("seed_demo")
        self.assertEqual(Theme.objects.filter(deleted_at__isnull=True).count(), first)
        self.assertGreaterEqual(first, 2)  # §G1: 2–3 seeded themes


# ---------------------------------------------------------------------------
# §K2 (Handoff #11) — the COMMITTED media-template zip round-trips
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=TMP_MEDIA)
class MediaTemplateZipTests(BaseCase):
    """/create links /question-media-template.zip; the file now exists in
    frontend/public (built from the template directory) and must import
    cleanly through the bulk upload — dry-run AND real — using the exact
    committed bytes. It contains a pipe row (TV|80s), so this doubles as a
    live multi-category check on the shipped template.

    #13 fix — environment-aware, invariant intact: the docker api image
    contains ONLY the backend, so inside the compose container the whole
    ``frontend/`` tree is legitimately absent and these tests SKIP (with a
    visible reason) rather than error. But if ``frontend/`` EXISTS and the
    zip is missing, that is the C5 packaging-loss scenario and the tests
    still FAIL — the skip keys off the tree, never off the file."""

    ZIP_PATH = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "frontend" / "public" / "question-media-template.zip"
    )

    def setUp(self):
        super().setUp()
        if not self.ZIP_PATH.parent.parent.exists():  # no frontend/ tree at all
            self.skipTest(
                "frontend/ tree not present in this environment (docker api "
                "container) — run these from a full repo checkout"
            )

    def upload(self, **data):
        self.auth(self.creator)
        payload = SimpleUploadedFile(
            "question-media-template.zip", self.ZIP_PATH.read_bytes(), content_type="application/zip"
        )
        return self.client.post(
            "/api/questions/bulk/", {"file": payload, **data}, format="multipart"
        )

    def test_committed_zip_exists_with_expected_members(self):
        self.assertTrue(self.ZIP_PATH.exists(), self.ZIP_PATH)
        import zipfile

        with zipfile.ZipFile(self.ZIP_PATH) as z:
            self.assertEqual(sorted(z.namelist()), ["media/example.png", "questions.csv"])

    def test_dry_run_then_real_import(self):
        # create_categories mirrors the /create form's "create missing
        # categories" checkbox — the template names (Movies, TV, 80s) don't
        # pre-exist for a fresh creator, and auto-create is the shipped flow.
        res = self.upload(dry_run="true", create_categories="true")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data.get("dry_run"))
        self.assertEqual(res.data["created"], 3)
        self.assertEqual(Question.objects.count(), 0)  # dry means dry

        res = self.upload(create_categories="true")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["created"], 3)
        self.assertEqual(Question.objects.count(), 3)
        # The pipe row landed in BOTH categories (auto-created).
        piped = Question.objects.get(question_text="Who shot J.R.?")
        self.assertEqual(
            sorted(piped.categories.values_list("name", flat=True)), ["80s", "TV"]
        )
        # The media row carries its image, sized through the pipeline.
        with_media = Question.objects.get(question_text="What film is this frame from?")
        self.assertTrue(with_media.image)
        self.assertGreater(with_media.media_bytes, 0)


# ---------------------------------------------------------------------------
# §G (Handoff #12) — the anonymous public category browse endpoint
# ---------------------------------------------------------------------------
class PublicCategoryBrowseTests(BaseCase):
    """GET /api/categories/public/ — AllowAny, counts only, exactly five
    keys, never any question content (rule 5), never any non-browsable
    category, and never swallowed by CategoryViewSet's detail route."""

    URL = "/api/categories/public/"

    def setUp(self):
        super().setUp()
        # Official category with a mix of questions: 2 official + 1 public-
        # approved creator question count; 1 soft-deleted and 1 private don't.
        self.official = Category.objects.create(
            owner=None, name="Movies",
            description="From the silents to last summer.",
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        for i in range(2):
            q = Question.objects.create(
                owner=None, question_text=f"Official {i}?", answer="SECRET-OFFICIAL-ANSWER",
                difficulty=1, visibility=Visibility.PUBLIC,
                moderation_status=ModerationStatus.APPROVED,
            )
            q.categories.add(self.official)
        approved = make_question(
            self.creator, self.official, "Approved creator Q?",
            status=ModerationStatus.APPROVED, visibility=Visibility.PUBLIC,
        )
        approved.answer = "SECRET-CREATOR-ANSWER"
        approved.save()
        deleted = make_question(
            self.creator, self.official, "Deleted Q?",
            status=ModerationStatus.APPROVED, visibility=Visibility.PUBLIC,
        )
        from django.utils import timezone as _tz

        deleted.deleted_at = _tz.now()
        deleted.save()
        make_question(self.creator, self.official, "Private Q?")  # never counts

        # A creator category that IS browsable (public + approved)…
        self.approved_cat = make_category(self.creator, "Beer", public_approved=True)
        # …and the full set that must be ABSENT:
        self.private_cat = make_category(self.creator, "My Private")
        self.pending_cat = Category.objects.create(
            owner=self.creator, name="Pending", visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.PENDING,
        )
        self.rejected_cat = Category.objects.create(
            owner=self.creator, name="Rejected", visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.REJECTED,
        )
        self.deleted_cat = Category.objects.create(
            owner=None, name="Gone", visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED, deleted_at=_tz.now(),
        )

    def names(self, res):
        return [c["name"] for c in res.json()["results"]]

    def test_anonymous_200_with_only_browsable_categories(self):
        res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 200, res.content)
        names = self.names(res)
        self.assertIn("Movies", names)
        self.assertIn("Beer", names)
        for absent in ("My Private", "Pending", "Rejected", "Gone"):
            self.assertNotIn(absent, names)

    def test_authenticated_200_too(self):
        # No perverse 403 for logged-in users browsing the shop window.
        self.auth(self.free)
        res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("Movies", self.names(res))

    def test_payload_is_exactly_the_five_public_keys(self):
        # This shape is now public API — pin it.
        res = self.client.get(self.URL)
        for row in res.json()["results"]:
            self.assertEqual(
                set(row), {"id", "name", "description", "photo", "question_count"}, row
            )

    def test_no_question_content_anywhere_in_the_payload(self):
        # Rule 5 stays airtight: counts only. Grep the raw payload for the
        # seeded answer strings AND question text (the SnapshotReveal pattern).
        raw = self.client.get(self.URL).content.decode()
        for leak in ("SECRET-OFFICIAL-ANSWER", "SECRET-CREATOR-ANSWER", "Official 0?", "Approved creator Q?"):
            self.assertNotIn(leak, raw)

    def test_question_count_is_browsable_actives_only(self):
        res = self.client.get(self.URL)
        by_name = {c["name"]: c for c in res.json()["results"]}
        # 2 official + 1 public-approved; the soft-deleted and the private
        # questions don't count.
        self.assertEqual(by_name["Movies"]["question_count"], 3)
        self.assertEqual(by_name["Beer"]["question_count"], 0)

    def test_ordering_is_name_a_to_z(self):
        res = self.client.get(self.URL)
        names = self.names(res)
        self.assertEqual(names, sorted(names))

    def test_route_not_swallowed_by_category_detail(self):
        # If the router's categories/<pk>/ matched first, anonymous would be
        # a 401 (IsAuthenticated viewset) — it must instead be this view's
        # public 200 (the history-precedence pattern).
        res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 200)
        self.assertIn("results", res.json())

    def test_stale_or_garbage_auth_header_still_200(self):
        # An anonymous marketing surface must never 401 on a dead Knox token
        # left in a browser (authentication_classes is empty on purpose).
        res = self.client.get(self.URL, HTTP_AUTHORIZATION="Token deadbeef")
        self.assertEqual(res.status_code, 200, res.content)


# ---------------------------------------------------------------------------
# Handoff #15 §F1 — server-side search + ?mine= (categories, questions, themes)
# ---------------------------------------------------------------------------

class CategorySearchTests(BaseCase):
    """`?search=` on both category lists: icontains over name OR description,
    stripped, silently capped at 100 chars, ADDITIVE (the unfiltered public
    call stays byte-identical — its shape/order/openness are pinned)."""

    PUBLIC = "/api/categories/public/"

    def setUp(self):
        super().setUp()
        self.movies = Category.objects.create(
            owner=None, name="Movies", description="From silents to sequels.",
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.beer = Category.objects.create(
            owner=None, name="Beer", description="Hops, malts and the cinema of brewing.",
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )

    def names(self, res):
        return [c["name"] for c in res.json()["results"]]

    def test_public_search_matches_name_or_description_case_insensitive(self):
        # Name match, case-insensitive.
        self.assertEqual(self.names(self.client.get(self.PUBLIC, {"search": "mOvIeS"})), ["Movies"])
        # Description match ("cinema" only appears in Beer's description).
        self.assertEqual(self.names(self.client.get(self.PUBLIC, {"search": "cinema"})), ["Beer"])
        # No match → empty page (200, count 0), never an error.
        res = self.client.get(self.PUBLIC, {"search": "zzz-nothing-here"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["count"], 0)
        # Whitespace-only search = no filter (strip → empty → default listing).
        self.assertIn("Movies", self.names(self.client.get(self.PUBLIC, {"search": "   "})))

    def test_public_search_is_silently_capped_at_100_chars(self):
        # A 100-char name; searching with 100 matching chars + garbage past
        # the cap still matches, proving the tail is silently dropped (no 400).
        long_name = "Z" * 100
        Category.objects.create(
            owner=None, name=long_name, visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED,
        )
        res = self.client.get(self.PUBLIC, {"search": long_name + "GARBAGE-PAST-THE-CAP"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.names(res), [long_name])

    def test_public_search_pages_deterministically(self):
        # 60 matches → page 1 carries 50 (DRF PAGE_SIZE) with next, page 2
        # the other 10; name-A→Z with id tiebreak keeps pages stable.
        for i in range(60):
            Category.objects.create(
                owner=None, name=f"PageProbe {i:02d}", visibility=Visibility.PUBLIC,
                moderation_status=ModerationStatus.APPROVED,
            )
        one = self.client.get(self.PUBLIC, {"search": "pageprobe"}).json()
        self.assertEqual((one["count"], len(one["results"])), (60, 50))
        self.assertIsNotNone(one["next"])
        two = self.client.get(self.PUBLIC, {"search": "pageprobe", "page": 2}).json()
        self.assertEqual(len(two["results"]), 10)
        self.assertIsNone(two["next"])
        got = [c["name"] for c in one["results"] + two["results"]]
        self.assertEqual(got, sorted(got))

    def test_public_unfiltered_call_is_untouched(self):
        # §B5: adding ?search= must not change the default response — the
        # bare call and an EMPTY search are byte-identical, rows keep the
        # exact five keys, ordering stays name-A→Z.
        bare = self.client.get(self.PUBLIC)
        empty = self.client.get(self.PUBLIC, {"search": ""})
        self.assertEqual(bare.content, empty.content)
        rows = bare.json()["results"]
        for row in rows:
            self.assertEqual(set(row), {"id", "name", "description", "photo", "question_count"}, row)
        names = [c["name"] for c in rows]
        self.assertEqual(names, sorted(names))

    def test_authed_search_respects_visibility_scoping(self):
        # `other`'s PRIVATE category matches the term but must stay invisible
        # to `creator` — the search filter narrows the scoped queryset, never
        # widens it.
        make_category(self.other, "ScopedSecret Movies")
        self.auth(self.creator)
        names = [c["name"] for c in self.client.get("/api/categories/", {"search": "movies"}).json()["results"]]
        self.assertEqual(names, ["Movies"])
        self.auth(self.other)
        names = [c["name"] for c in self.client.get("/api/categories/", {"search": "movies"}).json()["results"]]
        self.assertEqual(names, ["Movies", "ScopedSecret Movies"])

    def test_authed_list_orders_name_then_id(self):
        # #15 changed the authed list order from plain id to ("name", "id") —
        # a paged picker reads alphabetically like the public browse.
        make_category(self.creator, "Aardvarks")
        self.auth(self.creator)
        names = [c["name"] for c in self.client.get("/api/categories/").json()["results"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "Aardvarks")

    def test_mine_filters_to_own_rows_only(self):
        mine_cat = make_category(self.creator, "My Private Pile")
        make_category(self.other, "Other Private")
        self.auth(self.creator)
        data = self.client.get("/api/categories/", {"mine": "1"}).json()
        self.assertEqual([c["name"] for c in data["results"]], [mine_cat.name])
        self.assertEqual(data["count"], 1)  # the /create meters read this
        # Anonymous callers own nothing: empty page, not an error.
        self.client.force_authenticate(user=None)
        anon = self.client.get("/api/categories/", {"mine": "1"})
        self.assertEqual(anon.status_code, 200, anon.content)
        self.assertEqual(anon.json()["count"], 0)


class QuestionSearchTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.cat = make_category(None, "Movies", public_approved=True)
        self.joker = Question.objects.create(
            owner=None, question_text="Who played the Joker?", answer="Heath Ledger",
            difficulty=2, visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.joker.categories.add(self.cat)
        self.talkie = Question.objects.create(
            owner=None, question_text="First talkie premiere year?", answer="1927",
            difficulty=3, visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.talkie.categories.add(self.cat)

    def texts(self, res):
        return [q["question_text"] for q in res.json()["results"]]

    def test_search_matches_text_or_answer_case_insensitive(self):
        self.auth(self.creator)
        self.assertEqual(self.texts(self.client.get("/api/questions/", {"search": "jOkEr"})), ["Who played the Joker?"])
        # Answer-side match: "ledger" lives only in the answer.
        self.assertEqual(self.texts(self.client.get("/api/questions/", {"search": "ledger"})), ["Who played the Joker?"])
        self.assertEqual(self.client.get("/api/questions/", {"search": "zzz-none"}).json()["count"], 0)

    def test_search_respects_scoping_and_combines_with_category(self):
        secret = make_question(self.other, self.cat, "Secret Joker question?")  # other's PRIVATE
        self.auth(self.creator)
        ids = [q["id"] for q in self.client.get("/api/questions/", {"search": "joker"}).json()["results"]]
        self.assertEqual(ids, [self.joker.id])  # never other's private
        other_cat = make_category(None, "Music", public_approved=True)
        self.joker.categories.add(other_cat)
        # search + ?category= stack: joker matches "joker" but not in Music-only rows.
        ids = [
            q["id"]
            for q in self.client.get(
                "/api/questions/", {"search": "joker", "category": other_cat.id}
            ).json()["results"]
        ]
        self.assertEqual(ids, [self.joker.id])
        ids = [
            q["id"]
            for q in self.client.get(
                "/api/questions/", {"search": "talkie", "category": other_cat.id}
            ).json()["results"]
        ]
        self.assertEqual(ids, [])
        del secret  # only here to prove absence above

    def test_mine_filters_to_own_rows_with_true_count(self):
        mine = make_question(self.creator, self.cat, "My private question?")
        self.auth(self.creator)
        data = self.client.get("/api/questions/", {"mine": "1"}).json()
        self.assertEqual([q["id"] for q in data["results"]], [mine.id])
        self.assertEqual(data["count"], 1)

    def test_detail_requests_ignore_stray_search_params(self):
        # get_object routes through get_queryset — a query param on a detail
        # URL must never turn into a 404 (list-only filters, §F1).
        mine = make_question(self.creator, self.cat, "My private question?")
        self.auth(self.creator)
        res = self.client.get(f"/api/questions/{mine.id}/", {"search": "zzz-no-match", "mine": "1"})
        self.assertEqual(res.status_code, 200, res.content)


class ThemeSearchTests(BaseCase):
    def setUp(self):
        super().setUp()
        from .models import Theme

        self.movies = make_category(None, "Movies", public_approved=True)
        self.eighties = Theme.objects.create(name="80s Night", description="Neon and synths.")
        self.eighties.categories.add(self.movies)
        self.pub = Theme.objects.create(name="Pub Classics", description="The staples.")

    def test_host_theme_search_filters_and_keeps_the_pinned_shape(self):
        self.auth(self.free)
        rows = self.client.get("/api/themes/", {"search": "nEoN"}).json()
        self.assertEqual([t["name"] for t in rows], ["80s Night"])  # description match, case-insensitive
        for t in rows:
            self.assertEqual(set(t), {"id", "name", "description", "categories"}, t)
        self.assertEqual(self.client.get("/api/themes/", {"search": "zzz"}).json(), [])
        # Bare array in and out — a search must not grow a page envelope.
        self.assertIsInstance(self.client.get("/api/themes/").json(), list)

    def test_moderation_theme_search(self):
        self.auth(self.staff)
        rows = self.client.get("/api/moderation/themes/", {"search": "pub"}).json()
        self.assertEqual([t["name"] for t in rows], ["Pub Classics"])
        self.assertIsInstance(rows, list)  # bare array here too

    def test_moderation_theme_rows_carry_active_category_details(self):
        # §F7: {id, name} seeds for the edit picker — active members only,
        # name-sorted, and consistent with category_names.
        from django.utils import timezone as _tz

        dead = make_category(None, "Dead Cat", public_approved=True)
        self.eighties.categories.add(dead)
        dead.deleted_at = _tz.now()
        dead.save()
        self.auth(self.staff)
        row = next(t for t in self.client.get("/api/moderation/themes/").json() if t["id"] == self.eighties.id)
        self.assertEqual(row["category_details"], [{"id": self.movies.id, "name": "Movies"}])
        self.assertEqual(row["category_names"], ["Movies"])


# ---------------------------------------------------------------------------
# Handoff #15 §F2 — batch similar (the review aid without the herd)
# ---------------------------------------------------------------------------

class SimilarBatchTests(BaseCase):
    URL = "/api/moderation/questions/similar/batch/"

    def setUp(self):
        super().setUp()
        self.movies = make_category(None, "Movies", public_approved=True)
        self.music = make_category(None, "Music", public_approved=True)
        # A healthy Movies pool (>= MIN_CATEGORY_POOL approved siblings)…
        self.approved = []
        for i, text in enumerate(
            [
                "Which actor played the Joker in The Dark Knight?",
                "Which actor played Batman in The Dark Knight?",
                "What year did The Dark Knight premiere in cinemas?",
                "Who directed The Dark Knight trilogy of films?",
                "Which city stands in for Gotham in The Dark Knight?",
            ]
        ):
            q = Question.objects.create(
                owner=None, question_text=text, answer=f"A{i}", difficulty=2,
                visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
            )
            q.categories.add(self.movies)
            self.approved.append(q)
        # …and a thin Music pool (1 approved) so one target exercises the
        # global fallback path.
        self.music_only = Question.objects.create(
            owner=None, question_text="Which band scored Flash Gordon?", answer="Queen",
            difficulty=2, visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.music_only.categories.add(self.music)
        self.pending_movies = make_question(
            self.creator, self.movies, "Who played the Joker in The Dark Knight?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        self.pending_music = make_question(
            self.creator, self.music, "Which band scored the film Flash Gordon?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )

    def test_batch_output_equals_single_endpoint_for_every_target(self):
        # The parity contract, over a healthy-pool target, a thin-pool
        # (global fallback) target, an APPROVED target and a soft-deleted
        # one — same rows, same order, same shape, keyed by string id.
        from django.utils import timezone as _tz

        deleted = make_question(
            self.creator, self.movies, "Who was the Joker actor in The Dark Knight?",
            status=ModerationStatus.PENDING, visibility=Visibility.PUBLIC,
        )
        deleted.deleted_at = _tz.now()
        deleted.save()
        targets = [self.pending_movies, self.pending_music, self.approved[0], deleted]
        self.auth(self.staff)
        singles = {
            t.id: self.client.get(f"/api/moderation/questions/{t.id}/similar/").json()["similar"]
            for t in targets
        }
        res = self.client.post(self.URL, {"ids": [t.id for t in targets]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        batch = res.json()
        self.assertEqual(set(batch), {str(t.id) for t in targets})
        for t in targets:
            self.assertEqual(batch[str(t.id)], singles[t.id], f"parity broke for target {t.id}")
        # Sanity on the fixtures: the healthy target found its planted dupe,
        # the thin target actually crossed categories via the global fallback.
        self.assertEqual(batch[str(self.pending_movies.id)][0]["id"], self.approved[0].id)
        self.assertIn(self.music_only.id, [r["id"] for r in batch[str(self.pending_music.id)]])

    def test_batch_validation(self):
        self.auth(self.staff)
        for bad in ({}, {"ids": []}, {"ids": "nope"}, {"ids": [1] * 51}, {"ids": ["x"]}):
            res = self.client.post(self.URL, bad, format="json")
            self.assertEqual(res.status_code, 400, (bad, res.content))
        # Exactly 50 is fine (the cap is inclusive).
        res = self.client.post(self.URL, {"ids": [self.pending_movies.id] * 50}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_batch_is_staff_only(self):
        self.auth(self.creator)
        res = self.client.post(self.URL, {"ids": [self.pending_movies.id]}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_unknown_ids_are_omitted_not_fatal(self):
        self.auth(self.staff)
        res = self.client.post(self.URL, {"ids": [self.pending_movies.id, 999999]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(set(res.json()), {str(self.pending_movies.id)})


class QuestionCategoryNamesTests(BaseCase):
    """§F1 (#15): QuestionSerializer rows carry `category_names` (ACTIVE
    names, sorted) so /create can label rows without a category fetch-all;
    the staff ModerationQuestionSerializer keeps its deliberate divergence
    (ALL names, deleted included — the graveyard views need them)."""

    def setUp(self):
        super().setUp()
        self.movies = make_category(None, "Movies", public_approved=True)
        self.beer = make_category(None, "Beer", public_approved=True)
        self.q = make_question(self.creator, [self.movies, self.beer], "Two-home question?")

    def test_rows_carry_active_names_sorted(self):
        from django.utils import timezone as _tz

        self.auth(self.creator)
        row = next(r for r in self.client.get("/api/questions/", {"mine": "1"}).json()["results"] if r["id"] == self.q.id)
        self.assertEqual(row["category_names"], ["Beer", "Movies"])
        # Soft-delete one home: it drops from the OWNER-facing names…
        self.beer.deleted_at = _tz.now()
        self.beer.save()
        row = next(r for r in self.client.get("/api/questions/", {"mine": "1"}).json()["results"] if r["id"] == self.q.id)
        self.assertEqual(row["category_names"], ["Movies"])

    def test_moderation_rows_keep_deleted_names(self):
        from django.utils import timezone as _tz

        self.beer.deleted_at = _tz.now()
        self.beer.save()
        self.auth(self.staff)
        rows = self.client.get("/api/moderation/questions/?status=all").json()["results"]
        row = next(r for r in rows if r["id"] == self.q.id)
        # …but the staff library still names the dead home (divergence pinned).
        self.assertEqual(row["category_names"], ["Beer", "Movies"])
