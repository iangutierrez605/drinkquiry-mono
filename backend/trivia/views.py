from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounts.quotas import (
    batch_quota_denial,
    batch_question_creation_denial,
    batch_storage_quota_denial,
    question_creation_denial,
    question_unarchive_denial,
    quota_denial,
    storage_quota_denial,
)
from billing.access import (
    PACK_INACTIVE,
    pack_category_denial,
    pack_question_denial,
    pack_storage_denial,
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
        # §F2 (#18): archived questions (the venue shelf) leave the public
        # count too — question_count is a usability promise, and archived
        # rows aren't drawable. The pinned-count semantics tests extend.
        browsable_question = Q(
            questions__deleted_at__isnull=True, questions__is_archived=False
        ) & (
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
        # §F4 (#18): optional pack binding — `entitlement: <id>` in the
        # body creates an ADDITIONAL bound category for that pack (the
        # starter one comes from the webhook). Server-side truth: must be
        # the caller's, a bound-content kind, ACTIVE, and under the pack's
        # small category cap. Bound categories skip the account quotas
        # (their pack owns them); photo bytes draw on pack storage.
        ent_id = request.data.get("entitlement")
        if ent_id not in (None, ""):
            from billing.models import BOUND_KINDS, Entitlement

            entitlement = (
                Entitlement.objects.filter(pk=ent_id, user=request.user, kind__in=BOUND_KINDS)
                .select_related("source_subscription")
                .first()
            )
            if entitlement is None:
                return Response(
                    {"entitlement": ["No such pack."]}, status=status.HTTP_400_BAD_REQUEST
                )
            if not entitlement.is_active:
                return Response(dict(PACK_INACTIVE), status=status.HTTP_403_FORBIDDEN)
            if denial := pack_category_denial(entitlement):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
            if denial := pack_storage_denial(entitlement, _incoming_file_bytes(request)):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(entitlement=entitlement)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        # Quota gate before validation/uploads. Free plans have a limit of 0,
        # so this is also what blocks non-creators (structured 403, D2).
        if denial := quota_denial(request.user, "categories"):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3: storage quota on the incoming photo, through the shared helper.
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def _pack_gate(self, instance, user=None):
        """§F4: writes on a BOUND category require its pack ACTIVE. For an
        UNBOUND one, a lapsed-buyer caller re-denies here (see the question
        twin)."""
        if instance.entitlement_id is None:
            from billing.access import can_paid_write

            if user is not None and not can_paid_write(user):
                raise PermissionDenied
            return None
        if instance.entitlement.is_active:
            return None
        return dict(PACK_INACTIVE)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if denial := self._pack_gate(instance, request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3 on PATCH-with-file too. Conservative: the replaced file's old
        # bytes still count until the row saves — never under-enforces.
        # Bound categories draw on their pack's storage instead (#18).
        if instance.entitlement_id is not None:
            if denial := pack_storage_denial(instance.entitlement, _incoming_file_bytes(request)):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
        elif denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if denial := self._pack_gate(self.get_object(), request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

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
        # §F4 (#18): scope decides the lane. Bound target (any pack
        # category) → the PACK's budget/storage/activity govern and the
        # account quotas stand down; unbound → the account's two-lane check
        # (plain plan/overrides ∪ the venue 100-ACTIVE gate). The serializer
        # re-validates the scope shape; this pre-read only picks the lane —
        # a mixed-scope body falls through to its 400 there.
        bound = self._bound_entitlement_from_request(request)
        if bound is not None:
            entitlement, denial = bound
            if denial is not None:
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
            if denial := pack_question_denial(entitlement):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
            if denial := pack_storage_denial(entitlement, _incoming_file_bytes(request)):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
            return super().create(request, *args, **kwargs)
        if denial := question_creation_denial(request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3: storage quota on incoming media, through the shared helper.
        if denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def _bound_entitlement_from_request(self, request):
        """Resolve the create body's target pack, if any.

        Returns None (unbound / unresolvable — the serializer will speak),
        or (entitlement, denial) where denial is the PACK_INACTIVE payload
        when the pack lapsed (403), else None."""
        # Form-encoded bodies (multipart uploads) carry repeated `categories`
        # keys — QueryDict.get would return only the LAST one and silently
        # misroute a mixed-scope body into the wrong lane. getlist first.
        if hasattr(request.data, "getlist"):
            raw = request.data.getlist("categories")
        else:
            raw = request.data.get("categories")
        if isinstance(raw, (str, int)):
            raw = [raw]
        if not isinstance(raw, list):
            return None
        try:
            ids = [int(v) for v in raw]
        except (TypeError, ValueError):
            return None
        bound = list(
            Category.objects.filter(
                id__in=ids, deleted_at__isnull=True, entitlement__isnull=False
            ).select_related("entitlement__source_subscription")
        )
        if not bound:
            return None
        entitlement = bound[0].entitlement
        if entitlement.user_id != request.user.id:
            return None  # not theirs — accepts_questions_from will 400 it
        if not entitlement.is_active:
            return entitlement, dict(PACK_INACTIVE)
        return entitlement, None

    def _instance_pack_gate(self, instance, user=None):
        """§F4: writes on BOUND content require the pack ACTIVE — expired
        packs are read-only (content preserved, never deleted). For UNBOUND
        content, a caller who only passed the permission layer as a lapsed
        buyer re-denies here (the lapsed-creator read-only precedent)."""
        bound = [c for c in instance.categories.all() if c.entitlement_id is not None]
        if not bound:
            from billing.access import can_paid_write

            if user is not None and not can_paid_write(user):
                raise PermissionDenied
            return None
        entitlement = bound[0].entitlement
        if entitlement.is_active:
            return None
        return dict(PACK_INACTIVE)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if denial := self._instance_pack_gate(instance, request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F3 on PATCH-with-file (see CategoryViewSet.update). Bound media
        # answers to the pack's storage; unbound to the account's.
        bound = [c for c in instance.categories.all() if c.entitlement_id is not None]
        if bound:
            if denial := pack_storage_denial(bound[0].entitlement, _incoming_file_bytes(request)):
                return Response(denial, status=status.HTTP_403_FORBIDDEN)
        elif denial := storage_quota_denial(request.user, _incoming_file_bytes(request)):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if denial := self._instance_pack_gate(self.get_object(), request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        """§F6 (#18): shelf a question — it leaves every draw/usable surface
        but stays listed to its owner (reversible; NOT a delete). Owner-only
        via the object permission; idempotent."""
        question = self.get_object()
        if question.owner_id != request.user.id:
            return Response(
                {"detail": "Only the owner can archive a question."}, status=status.HTTP_403_FORBIDDEN
            )
        if denial := self._instance_pack_gate(question, request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        if not question.is_archived:
            question.is_archived = True
            question.save(update_fields=["is_archived", "updated_at"])
        return Response(self.get_serializer(question).data)

    @action(detail=True, methods=["post"], url_path="unarchive")
    def unarchive(self, request, pk=None):
        """§F6: THE venue choke point — 100 active already → the structured
        quota_active_questions 403 (house shape)."""
        question = self.get_object()
        if question.owner_id != request.user.id:
            return Response(
                {"detail": "Only the owner can unarchive a question."}, status=status.HTTP_403_FORBIDDEN
            )
        if denial := self._instance_pack_gate(question, request.user):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        if question.is_archived:
            bound = any(c.entitlement_id is not None for c in question.categories.all())
            if not bound:
                if denial := question_unarchive_denial(request.user):
                    return Response(denial, status=status.HTTP_403_FORBIDDEN)
            question.is_archived = False
            question.save(update_fields=["is_archived", "updated_at"])
        return Response(self.get_serializer(question).data)

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

            # §F4 (#18): partition rows by SCOPE before the batch checks. A
            # row targeting a pack-bound category must be single-scope (all
            # its categories bound to ONE pack, no brand-new categories,
            # private only) — violations join parsed-style row errors. Bound
            # rows answer to their pack's budget/storage and leave the
            # account math; unbound rows keep the account checks (questions
            # via the §F6 two-lane union).
            scope_errors: list[dict] = []
            per_entitlement: dict[int, dict] = {}
            unbound_rows = 0
            unbound_media_bytes = 0
            if not official:
                for index, row in enumerate(parsed.rows):
                    row_num = index + 2  # 1-based incl. header, parser convention
                    cats = row["categories"]
                    ents = {
                        c.entitlement_id
                        for c in cats
                        if not isinstance(c, tuple) and c.entitlement_id is not None
                    }
                    has_unbound = any(
                        isinstance(c, tuple) or c.entitlement_id is None for c in cats
                    )
                    row_bytes = 0
                    if row["media_type"] != "none" and parsed.archive is not None:
                        info = None
                        try:
                            info = parsed.archive.getinfo(row["media_file"])
                        except KeyError:
                            pass
                        if info is not None:
                            row_bytes = info.file_size
                    if not ents:
                        unbound_rows += 1
                        unbound_media_bytes += row_bytes
                        continue
                    if has_unbound or len(ents) > 1:
                        scope_errors.append(
                            {
                                "row": row_num,
                                "field": "category",
                                "message": "A row can't mix pack categories with other categories.",
                            }
                        )
                        continue
                    if row["visibility"] != Visibility.PRIVATE:
                        scope_errors.append(
                            {
                                "row": row_num,
                                "field": "visibility",
                                "message": "Pack questions stay private — use visibility blank or private.",
                            }
                        )
                        continue
                    ent_id = next(iter(ents))
                    bucket = per_entitlement.setdefault(ent_id, {"rows": 0, "bytes": 0})
                    bucket["rows"] += 1
                    bucket["bytes"] += row_bytes
                if scope_errors:
                    return Response(
                        {"created": 0, "errors": scope_errors, "skipped": parsed.skipped},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if per_entitlement:
                    from billing.models import Entitlement

                    entitlements = {
                        e.pk: e
                        for e in Entitlement.objects.filter(
                            pk__in=per_entitlement
                        ).select_related("source_subscription")
                    }
                    for ent_id, bucket in per_entitlement.items():
                        entitlement = entitlements.get(ent_id)
                        if entitlement is None or entitlement.user_id != request.user.id:
                            # accepts_questions_from should have blocked a
                            # foreign bound category (bound = private); stay
                            # server-honest anyway.
                            return Response(
                                {"detail": "That pack isn't yours."},
                                status=status.HTTP_403_FORBIDDEN,
                            )
                        if not entitlement.is_active:
                            return Response(
                                dict(PACK_INACTIVE), status=status.HTTP_403_FORBIDDEN
                            )
                        if denial := pack_question_denial(entitlement, bucket["rows"]):
                            denial["requested"] = bucket["rows"]
                            return Response(denial, status=status.HTTP_403_FORBIDDEN)
                        if denial := pack_storage_denial(entitlement, bucket["bytes"]):
                            return Response(denial, status=status.HTTP_403_FORBIDDEN)

            # Whole-batch quota checks (official content is quota-exempt:
            # owner-less rows never count against anyone). Categories first,
            # then questions — the first denial wins, each with its own code
            # and `requested`. Applied on dry runs too, so a free user learns
            # about the 403 before fixing 60 rows of a file.
            if not official:
                if parsed.new_categories:
                    if denial := batch_quota_denial(request.user, "categories", len(parsed.new_categories)):
                        return Response(denial, status=status.HTTP_403_FORBIDDEN)
                if unbound_rows:
                    # §F6 two-lane union for the account-scoped slice only.
                    if denial := batch_question_creation_denial(request.user, unbound_rows):
                        return Response(denial, status=status.HTTP_403_FORBIDDEN)
                # §F3: whole-batch storage check — `requested` is the total
                # uncompressed bytes of the media this file would store
                # (counted per row, matching what create_rows stores; a file
                # referenced by several rows is stored once per row). Bound
                # rows' bytes were checked against their pack above (#18).
                if unbound_media_bytes:
                    if denial := batch_storage_quota_denial(request.user, unbound_media_bytes):
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
