from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.quotas import (
    batch_quota_denial,
    batch_storage_quota_denial,
    quota_denial,
    storage_quota_denial,
)

from .bulk_upload import create_new_categories, create_rows, parse_bulk_upload

from .models import Category, ModerationStatus, Question, QuestionReport, Visibility
from .permissions import IsCreator, IsOwnerOrReadOnlyPublic
from .serializers import CategorySerializer, PublicCategorySerializer, QuestionSerializer

PUBLIC_APPROVED = Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)


def _incoming_file_bytes(request) -> int:
    """Total size of the request's uploaded files (§F3 storage check input).
    Pre-resize sizes — a conservative upper bound on what will be stored."""
    return sum(f.size for f in request.FILES.values())


def search_param(request) -> str:
    """§F1 (Handoff #15): the ONE reading of `?search=` — stripped and
    SILENTLY capped at 100 chars (a pasted paragraph just searches on its
    first 100 chars instead of 400ing; nothing meaningful is that long).
    Empty string means "no filter" — the default listing, never an empty
    result set. Shared by every search-capable list in trivia (categories
    public + authed, questions, both theme surfaces)."""
    return request.query_params.get("search", "").strip()[:100]


class PublicCategoryListView(generics.ListAPIView):
    """§G1 (Handoff #12): `GET /api/categories/public/` — the logged-out
    marketing funnel. AllowAny, list-only, and DELIBERATELY UNTHROTTLED
    (it joins game join + the polling snapshot on the pinned unthrottled
    list; §F's bot gates sit on the anonymous WRITE surface, not here).

    Queryset: active categories that are OFFICIAL (owner is null) or
    PUBLIC + APPROVED — never private/pending/rejected/deleted (each
    absence pinned). `question_count` counts ACTIVE questions that are
    themselves official or public-approved, annotated in one query (do NOT
    reuse the per-user usable_question_count machinery — it takes a user;
    this surface has none). Ordering name A→Z with an id tiebreak
    (deterministic pages, §G house rule). Registered BEFORE trivia's router
    include (static-before-parameterized, house rule) so the CategoryViewSet
    detail route can't swallow /public/ as a pk.

    §F1 (Handoff #15): `?search=` — icontains on name OR description
    (shared search_param semantics: stripped, silently capped at 100).
    ADDITIVE ONLY: the unfiltered call's shape, ordering, AllowAny and
    unthrottled status are all pinned (smoke + tests). Staying unthrottled
    with an anon-reachable icontains is a MEASURED call, not an oversight:
    at the current thousands-scale a seq scan over ~100-char names +
    short descriptions is single-digit ms; if the corpus grows another
    order of magnitude, the §M escape hatch is a pg_trgm GIN index — never
    a throttle on this pinned-unthrottled surface.
    """

    serializer_class = PublicCategorySerializer
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()  # anonymous surface: a stale Knox header must never 401 it

    def get_queryset(self):
        browsable_question = Q(questions__deleted_at__isnull=True) & (
            Q(questions__owner__isnull=True)
            | Q(
                questions__visibility=Visibility.PUBLIC,
                questions__moderation_status=ModerationStatus.APPROVED,
            )
        )
        qs = (
            Category.objects.filter(deleted_at__isnull=True)
            .filter(Q(owner__isnull=True) | PUBLIC_APPROVED)
            .annotate(question_count=Count("questions", filter=browsable_question, distinct=True))
        )
        if search := search_param(self.request):
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
        return qs.order_by("name", "id")


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = (IsCreator, IsOwnerOrReadOnlyPublic)

    def create(self, request, *args, **kwargs):
        # Quota gate before validation/uploads. Free plans have a limit of 0,
        # so this is also what blocks non-creators (structured 403, D2).
        if denial := quota_denial(request.user, "categories"):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3: storage quota on the incoming photo, through the shared helper.
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        # §F3 on PATCH-with-file too. Conservative: the replaced file's old
        # bytes still count until the row saves — never under-enforces.
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """§F5 (Handoff #10): category delete is a SOFT delete — set
        deleted_at, return the usual 204. Same uniform rule as questions
        (§I2 #9): hard-deleting an ever-PLAYED category was a guaranteed
        ProtectedError 500 (BoardColumn.category is PROTECT), and the M2M
        removed the old questions-cascade anyway. The category's QUESTIONS
        are NOT cascaded: a question whose categories are all deleted simply
        becomes undrawable but stays in its owner's list, recategorizable
        via PATCH. Board columns/snapshots/reports/history keep the row;
        quota counters exclude it. Restore is punted (§M)."""
        from django.utils import timezone

        instance.deleted_at = timezone.now()
        instance.save(update_fields=["deleted_at", "updated_at"])

    def get_queryset(self):
        user = self.request.user
        # Ordering ("name", "id"): #15 changed the LIST order from plain id
        # to name-A→Z so a paged create-grid/picker reads alphabetically like
        # the public browse; the id tiebreak keeps pages deterministic (the
        # compose run's UnorderedObjectListWarning stays fixed). Applied here
        # in the queryset, not as Meta.ordering, so no other Category caller
        # changes behavior. Nothing consumed the old id-order semantically —
        # every list consumer was rewritten around server pages this session.
        # §F5: deleted categories are invisible here for everyone —
        # get_object routes through this too, so a deleted category 404s on
        # retrieve/PATCH/DELETE.
        qs = Category.objects.filter(deleted_at__isnull=True)
        if user.is_authenticated:
            qs = qs.filter(PUBLIC_APPROVED | Q(owner=user) | Q(owner__isnull=True)).distinct()
        else:
            qs = qs.filter(PUBLIC_APPROVED | Q(owner__isnull=True))
        # §F1 (#15): list-only filters — a detail request (get_object routes
        # through here) must never 404 because of a stray query param.
        if self.action == "list":
            if search := search_param(self.request):
                qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
            # ?mine= — the /create "My content" pager: the caller's OWN rows
            # only (any visibility/status). Anonymous callers own nothing, so
            # mine on an anon list is an empty page, not an error.
            if self.request.query_params.get("mine"):
                qs = qs.filter(owner=user) if user.is_authenticated else qs.none()
        return qs.order_by("name", "id")


class QuestionViewSet(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    permission_classes = (IsCreator, IsOwnerOrReadOnlyPublic)

    def get_permissions(self):
        # §K2 (Handoff #8): flagging a bad question is for ANY authenticated
        # host — it isn't content creation, so IsCreator (a paid-plan gate on
        # non-create writes) must not apply. Visibility scoping still holds:
        # get_object goes through get_queryset, so a user can only flag
        # questions they can see.
        if getattr(self, "action", None) == "report":
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="report")
    def report(self, request, pk=None):
        """§K2: flag a question as "not good" for moderator review. Filing a
        report NEVER changes moderation_status — the question stays listed
        and playable until a moderator acts from /moderate's Flagged tab
        (that's the feature's explicit ask). One OPEN report per (question,
        reporter) — the DB partial unique constraint is the rate limiter; a
        second POST while one is open gets a 409 (pinned)."""
        question = self.get_object()
        reason = str(request.data.get("reason") or "").strip()[:2000]
        try:
            with transaction.atomic():
                report = QuestionReport.objects.create(
                    question=question, reporter=request.user, reason=reason
                )
        except IntegrityError:
            # Caught OUTSIDE the atomic block (house pattern) when the partial
            # unique constraint (one open report per reporter) fires.
            return Response(
                {"detail": "You've already flagged this question — a moderator will review it."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {"id": report.id, "question": question.id, "status": report.status},
            status=status.HTTP_201_CREATED,
        )

    def create(self, request, *args, **kwargs):
        if denial := quota_denial(request.user, "questions"):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3: storage quota on incoming media, through the shared helper.
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        # §F3 on PATCH-with-file (see CategoryViewSet.update).
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        """Bulk import — plain CSV or zip-with-media (Handoff #4 §G2, #5 §F/§G).

        Parse and validate ALL rows first, then all-or-nothing in one
        transaction — a half-imported file that consumed quota is worse than
        "fix row 37 and re-upload". All-or-nothing spans both models: any row
        error means zero categories AND zero questions. Row numbers in errors
        are 1-based including the header (first data row = 2).
        """
        def flag(name, default):
            return str(request.data.get(name, default)).strip().lower() in ("1", "true", "yes", "on")

        official = flag("official", "false")
        dry_run = flag("dry_run", "false")
        skip_duplicates = flag("skip_duplicates", "true")
        create_categories = flag("create_categories", "false")  # §F, default = v1 behavior

        if official and not request.user.is_staff:
            return Response(
                {"detail": "Only staff can upload official content."}, status=status.HTTP_403_FORBIDDEN
            )
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"detail": "Attach a CSV or zip file in the `file` field."}, status=status.HTTP_400_BAD_REQUEST
            )

        parsed = parse_bulk_upload(
            uploaded, request.user,
            official=official, skip_duplicates=skip_duplicates, create_categories=create_categories,
        )
        try:
            if parsed.file_error:
                return Response({"detail": parsed.file_error}, status=status.HTTP_400_BAD_REQUEST)
            if parsed.errors:
                return Response(
                    {"created": 0, "errors": parsed.errors, "skipped": parsed.skipped},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            category_names = parsed.category_names

            # Whole-batch quota checks (official content is quota-exempt:
            # owner-less rows never count against anyone). Categories first,
            # then questions — the first denial wins, each with its own code
            # and `requested`. Applied on dry runs too, so a free user learns
            # about the 403 before fixing 60 rows of a file.
            if not official:
                if parsed.new_categories:
                    if denial := batch_quota_denial(request.user, "categories", len(parsed.new_categories)):
                        return Response(denial, status=status.HTTP_403_FORBIDDEN)
                if parsed.rows:
                    if denial := batch_quota_denial(request.user, "questions", len(parsed.rows)):
                        return Response(denial, status=status.HTTP_403_FORBIDDEN)
                # §F3: whole-batch storage check — `requested` is the total
                # uncompressed bytes of the media this file would store
                # (counted per row, matching what create_rows stores; a file
                # referenced by several rows is stored once per row).
                if parsed.media_bytes_total:
                    if denial := batch_storage_quota_denial(request.user, parsed.media_bytes_total):
                        return Response(denial, status=status.HTTP_403_FORBIDDEN)

            if dry_run:
                # Nothing written — reports what WOULD happen, media included.
                return Response(
                    {
                        "created": len(parsed.rows), "errors": [], "skipped": parsed.skipped,
                        "dry_run": True,
                        "categories_created": len(category_names), "category_names": category_names,
                        "media_files": parsed.media_files,
                    }
                )

            try:
                with transaction.atomic():
                    category_map = create_new_categories(
                        parsed.new_categories, request.user, official=official
                    )
                    created = create_rows(
                        parsed.rows, request.user,
                        official=official, category_map=category_map, archive=parsed.archive,
                    )
            except IntegrityError:
                # Concurrent upload raced unique_category_name_per_owner (§F:
                # out of scope beyond a clean 400 — never a 500).
                return Response(
                    {"detail": "A category in this file was created by another upload at the same time — try again."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {
                    "created": created, "errors": [], "skipped": parsed.skipped, "dry_run": False,
                    "categories_created": len(category_names), "category_names": category_names,
                },
                status=status.HTTP_201_CREATED,
            )
        finally:
            if parsed.archive is not None:
                parsed.archive.close()

    def perform_destroy(self, instance):
        """§I2 (Handoff #9): owner delete is a SOFT delete — set deleted_at,
        return the usual 204. Uniform rule, no "hard delete if never played"
        special case: hard-deleting an ever-played question was a guaranteed
        ProtectedError 500 (BoardCell.question is PROTECT), and one rule is
        simpler. The row (and its media files) stays so game history, reports
        and mid-game snapshots keep working; quota counters exclude deleted
        rows, so the owner's slots free up. Restore is punted (§M)."""
        from django.utils import timezone

        instance.deleted_at = timezone.now()
        instance.save(update_fields=["deleted_at", "updated_at"])

    def get_queryset(self):
        user = self.request.user
        # order_by("id"): deterministic pagination — this list view is what
        # tripped the compose run's UnorderedObjectListWarning (unordered
        # pagination can duplicate/skip rows across pages). Scoped to the list
        # queryset rather than Meta.ordering so no other Question caller
        # (board building, bulk dedupe, moderation) changes behavior.
        # §I: soft-deleted questions are invisible here for everyone —
        # get_object routes through this too, so a deleted question 404s on
        # retrieve/PATCH/DELETE/report. The one surface that can see deleted
        # rows is the staff moderation library (?deleted=, trivia/moderation).
        # §F4: category is an M2M now — prefetch it, filter through it, and
        # keep the paginated queryset duplicate-free (.distinct(); the M2M
        # join can otherwise repeat rows).
        qs = Question.objects.prefetch_related("categories").filter(deleted_at__isnull=True)
        if category_id := self.request.query_params.get("category"):
            qs = qs.filter(categories__id=category_id)
        # §F1 (#15): list-only filters — get_object routes through here, and
        # a detail request must never 404 on a stray query param.
        if self.action == "list":
            if search := search_param(self.request):
                qs = qs.filter(Q(question_text__icontains=search) | Q(answer__icontains=search))
            # ?mine= — the /create "My content" pager (see CategoryViewSet).
            if self.request.query_params.get("mine"):
                qs = qs.filter(owner=user) if user.is_authenticated else qs.none()
        if user.is_authenticated:
            return qs.filter(PUBLIC_APPROVED | Q(owner=user) | Q(owner__isnull=True)).distinct().order_by("id")
        return qs.filter(PUBLIC_APPROVED | Q(owner__isnull=True)).distinct().order_by("id")
