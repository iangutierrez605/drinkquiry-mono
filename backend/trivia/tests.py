"""Moderation review queue + bulk upload tests (Handoff #4 §H, #5 §H).

Covers the review queue, bulk CSV v1, §F auto-created categories, and §G
zip-with-media. Run: `python manage.py test accounts trivia games` (and, per the
environment-parity rule, once through docker compose against Postgres before
shipping).
"""
import io
import os
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
    return Question.objects.create(
        owner=owner,
        category=category,
        question_text=text,
        answer="A",
        difficulty=1,
        visibility=visibility,
        moderation_status=status,
        moderation_note=note,
    )


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
        self.assertEqual(rows[0]["category_name"], "Movies")
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
        rows = Question.objects.filter(category=self.official_cat)
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
                "category": self.cat.id,
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
            {"category": self.cat.id, "question_text": f"Q{Question.objects.count()}?",
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
            owner=None, category=self.cat,
            question_text="Which actor played the Joker in The Dark Knight?",
            answer="Heath Ledger", difficulty=2,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
        self.unrelated = Question.objects.create(
            owner=None, category=self.cat,
            question_text="What year did the first talkie premiere?",
            answer="1927", difficulty=3,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )
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
            owner=self.other, category=self.cat,
            question_text="Approved but suspicious?", answer="Yes", difficulty=1,
            visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED,
        )

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
        column = BoardColumn.objects.create(game=game, category=question.category, position=0)
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
        self.assertEqual(new.category_id, self.cat.id)
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
            owner=None, category=self.official_cat, question_text="Official filler?",
            answer="Yes", difficulty=1, visibility=Visibility.PUBLIC,
            moderation_status=ModerationStatus.APPROVED,
        )
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
