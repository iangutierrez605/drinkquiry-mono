"""Staff-only moderation review API (Handoff #4 §F1, extended by #8 §J1/§K).

Endpoints (all `IsAdminUser` — the frontend's is_staff gate is cosmetic):

  GET  /api/moderation/categories/            paginated, ?status= (default pending),
  GET  /api/moderation/questions/             oldest-first by created_at
  POST /api/moderation/{kind}/{id}/approve/   PENDING only (else 409); clears note
  POST /api/moderation/{kind}/{id}/reject/    PENDING only; requires {"note": "..."}
  GET  /api/moderation/questions/{id}/similar/  §K1: top near-duplicates among
                                              approved questions (review aid)
  GET  /api/moderation/counts/                {"categories": n, "questions": n} pending
                                              (shape pinned pre-#8; the Flagged
                                              tab counts from its own list)
  GET  /api/moderation/flags/                 §K2: questions with OPEN reports
  POST /api/moderation/flags/{question_id}/resolve/
                                              {"action": "dismiss"} keeps the
                                              question; {"action": "reject",
                                              "note": "..."} applies the same
                                              field semantics as the queue's
                                              reject (status=rejected + note).
                                              Both resolve all open reports.

Handoff #9 additions:

  GET  /api/moderation/questions/             now also the LIBRARY backend
                                              (§I4): ?search= (text/answer
                                              icontains), ?category=<id>,
                                              ?status= gains "all",
                                              ?owner= (email icontains,
                                              "official" = owner-less),
                                              ?deleted=active|only|all
                                              (default active — the ONE staff
                                              surface where deleted rows are
                                              visible), ?ordering= whitelist
                                              (created_at / usage_count, ±),
                                              page_size 25.
  POST /api/moderation/questions/{id}/delete/ §I2 staff soft delete (any
                                              owner's); 409 if already
                                              deleted; resolves open flags.
  POST /api/moderation/questions/{id}/revise/ §I3 versioned edit: NEW
                                              approved row with the edits,
                                              old row soft-deleted +
                                              replaced_by set, open flags
                                              resolved. Games already built
                                              keep the OLD text (their cells
                                              point at the old row —
                                              deliberate; no mid-game swaps).

Approving/rejecting now emails the owner (§K3, accounts/emails.py) — skipped
for official content and staff self-review; a send failure never breaks the
action.

Approving a category does NOT touch its questions — every item is vetted
separately. Double-acting reviewers get a 409 (no locking; last write would
win otherwise, and a plain status check is enough for a two-person team).
§K deliberately does not reshape those approve/reject mechanics — the flag
flow resolves through its own endpoint, applying the same fields.
"""
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import mixins, pagination, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.emails import send_moderation_outcome_email

from .models import Category, ModerationStatus, Question, QuestionReport, ReportStatus, Visibility
from .serializers import (
    FlaggedQuestionSerializer,
    ModerationCategorySerializer,
    ModerationQuestionSerializer,
)
from .similarity import similar_questions

VALID_STATUSES = {choice.value for choice in ModerationStatus}


class _ModerationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = (IsAdminUser,)
    model = None  # set by subclass

    def get_queryset(self):
        qs = self.model.objects.select_related("owner").order_by("created_at")
        if self.action != "list":
            return qs  # detail/actions must see any status so non-pending → 409, not 404
        wanted = self.request.query_params.get("status", ModerationStatus.PENDING)
        # §I4: "all" skips the status filter (the library's default view).
        # A NEW accepted value — the queue's default (pending) is unchanged.
        if wanted == "all":
            return qs
        if wanted not in VALID_STATUSES:
            # Raised as a DRF ValidationError → clean 400 with the valid choices.
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"status": f"Unknown status. One of: {', '.join(sorted(VALID_STATUSES | {'all'}))}."}
            )
        return qs.filter(moderation_status=wanted)

    def _act(self, request, *, approve: bool):
        obj = self.get_object()
        # §I2/§F5: a soft-deleted item can't be reviewed — questions since
        # #9, categories since #10 (both models carry deleted_at now).
        if obj.deleted_at is not None:
            return Response(
                {"detail": "This item was deleted — nothing to review."},
                status=status.HTTP_409_CONFLICT,
            )
        if obj.moderation_status != ModerationStatus.PENDING:
            return Response(
                {"detail": "Only pending items can be actioned — someone may have reviewed this already."},
                status=status.HTTP_409_CONFLICT,
            )
        if approve:
            obj.moderation_status = ModerationStatus.APPROVED
            obj.moderation_note = ""  # clear any stale note from an earlier rejection
        else:
            note = str(request.data.get("note") or "").strip()
            if not note:
                return Response({"note": ["A rejection note is required."]}, status=status.HTTP_400_BAD_REQUEST)
            obj.moderation_status = ModerationStatus.REJECTED
            obj.moderation_note = note
        obj.save(update_fields=["moderation_status", "moderation_note", "updated_at"])
        # §K3: tell the owner. Skips official content and self-review inside
        # the helper; a send failure logs a warning and never breaks the 200.
        send_moderation_outcome_email(obj, approved=approve, actor=request.user)
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._act(request, approve=True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._act(request, approve=False)


class ModerationCategoryViewSet(_ModerationViewSet):
    model = Category
    serializer_class = ModerationCategorySerializer

    def get_queryset(self):
        # §J1: usage = games that included the category (BoardColumn is unique
        # per (game, category), so counting columns counts games).
        qs = super().get_queryset().annotate(usage_count=Count("boardcolumn", distinct=True))
        # §F5 (Handoff #10): deleted categories never appear in the pending
        # queue list; detail/actions still see them so a review race gets the
        # shared 409, not a 404.
        if self.action == "list":
            qs = qs.filter(deleted_at__isnull=True)
        return qs


class LibraryPagination(pagination.PageNumberPagination):
    """§I4/§J2: explicit page size for the staff library surfaces — real
    pagination is the point ("we may have thousands"); the frontend renders
    prev/next from {results, count, next, previous} and never uses the
    allPages helper here."""

    page_size = 25


LIBRARY_ORDERINGS = ("created_at", "-created_at", "usage_count", "-usage_count")


class ModerationQuestionViewSet(_ModerationViewSet):
    model = Question
    serializer_class = ModerationQuestionSerializer
    pagination_class = LibraryPagination

    def get_queryset(self):
        qs = super().get_queryset()
        # §J1: usage = cells referencing the question, across all games.
        # §F4: `categories` is an M2M — prefetched for category_names.
        qs = qs.prefetch_related("categories").annotate(usage_count=Count("boardcell", distinct=True))
        if self.action != "list":
            return qs  # detail/actions see deleted rows too (revise/delete 409 cleanly)

        params = self.request.query_params
        from rest_framework.exceptions import ValidationError

        # §I4 ?deleted= — default "active" keeps the pending queue exactly as
        # it was; "only"/"all" make this the ONE staff surface where deleted
        # rows are visible (with deleted_at + replaced_by in the serializer).
        deleted = params.get("deleted", "active")
        if deleted == "active":
            qs = qs.filter(deleted_at__isnull=True)
        elif deleted == "only":
            qs = qs.filter(deleted_at__isnull=False)
        elif deleted != "all":
            raise ValidationError({"deleted": "One of: active, only, all."})

        if search := params.get("search", "").strip():
            qs = qs.filter(Q(question_text__icontains=search) | Q(answer__icontains=search))
        if category := params.get("category", "").strip():
            if not category.isdigit():
                raise ValidationError({"category": "Must be a category id."})
            # §F4: through the M2M; .distinct() keeps the PAGINATED queryset
            # duplicate-free (the join can otherwise repeat rows).
            qs = qs.filter(categories__id=category).distinct()
        # ?owner= — email icontains; the special value "official" means
        # owner-less (site-provided) content.
        if owner := params.get("owner", "").strip():
            qs = qs.filter(owner__isnull=True) if owner == "official" else qs.filter(owner__email__icontains=owner)

        if ordering := params.get("ordering", "").strip():
            if ordering not in LIBRARY_ORDERINGS:
                raise ValidationError({"ordering": f"One of: {', '.join(LIBRARY_ORDERINGS)}."})
            qs = qs.order_by(ordering, "id")  # id tiebreak: deterministic pages
        return qs

    @action(detail=True, methods=["get"])
    def similar(self, request, pk=None):
        """§K1: nearest existing approved matches for this question — a
        sub-endpoint rather than a `similar` block on the list serializer so
        the queue page stays snappy (the frontend fetches per card). A review
        AID, not auto-rejection; works for any status so the Flagged tab can
        reuse it on approved questions."""
        return Response({"similar": similar_questions(self.get_object())})

    @action(detail=True, methods=["post"], url_path="delete")
    def soft_delete(self, request, pk=None):
        """§I2 staff delete: soft-deletes ANY owner's question (the
        owner-scoped QuestionViewSet.destroy won't do for staff cleanup).
        409 if already deleted (double-act guard, same flavor as
        approve/reject). Open flags are resolved here — the report rows
        persist as history, but a deleted question must not linger in the
        Flagged tab."""
        question = self.get_object()
        if question.deleted_at is not None:
            return Response(
                {"detail": "Already deleted — someone may have beaten you to it."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            question.deleted_at = timezone.now()
            question.save(update_fields=["deleted_at", "updated_at"])
            question.reports.filter(status=ReportStatus.OPEN).update(status=ReportStatus.RESOLVED)
        return Response(self.get_serializer(question).data)

    @action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        """§I3 versioned edit: never mutate a question's text in place —
        create a NEW row carrying the edits and soft-delete the old one,
        linking old.replaced_by = new.

        Accepts any subset of {question_text, answer, difficulty,
        visibility}. Media re-upload is punted (§M): the new row keeps the
        old row's media FILES BY REFERENCE — two rows, one stored file, so a
        future hard-purge must never delete files still referenced by a
        replacement (documented in CHANGES.md).

        The new row is created APPROVED: a staff member just authored/blessed
        this exact text, so staff edits don't re-enter their own queue
        (delegated default — flip `moderation_status` below to PENDING to
        change it). Old open flags are resolved (the complaint was presumably
        the reason for the edit).

        Games already built keep the OLD text — their cells point at the old
        row, and a mid-game text swap under players would be worse than a
        stale question. New games draw the new row only.
        """
        old = self.get_object()
        if old.deleted_at is not None:
            return Response(
                {"detail": "This question was already deleted or revised — refresh the library."},
                status=status.HTTP_409_CONFLICT,
            )
        errors = {}
        edits = {}
        data = request.data
        if "question_text" in data:
            text = str(data["question_text"] or "").strip()
            if not text:
                errors["question_text"] = ["Cannot be blank."]
            edits["question_text"] = text
        if "answer" in data:
            answer = str(data["answer"] or "").strip()
            if not answer:
                errors["answer"] = ["Cannot be blank."]
            elif len(answer) > 500:
                errors["answer"] = ["500 characters max."]
            edits["answer"] = answer
        if "difficulty" in data:
            try:
                difficulty = int(data["difficulty"])
                if not 1 <= difficulty <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                errors["difficulty"] = ["Must be an integer from 1 to 5."]
            else:
                edits["difficulty"] = difficulty
        if "visibility" in data:
            visibility = str(data["visibility"] or "")
            if visibility not in {c.value for c in Visibility}:
                errors["visibility"] = [f"One of: {', '.join(c.value for c in Visibility)}."]
            edits["visibility"] = visibility
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        if not edits:
            return Response(
                {"detail": "Nothing to revise — send at least one of question_text, answer, difficulty, visibility."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            new = Question(
                owner=old.owner,
                question_text=old.question_text,
                answer=old.answer,
                difficulty=old.difficulty,
                visibility=old.visibility,
                media_type=old.media_type,
                # By-NAME assignment = by-reference copy: the file is not
                # re-read, re-processed or re-stored (it's committed).
                image=old.image.name if old.image else None,
                audio=old.audio.name if old.audio else None,
                video=old.video.name if old.video else None,
                moderation_status=ModerationStatus.APPROVED,
                moderation_note="",
            )
            for field, value in edits.items():
                setattr(new, field, value)
            new.save()
            # §F2: the revision lives in the SAME categories as the original
            # (recategorizing stays a normal owner/staff PATCH concern; the
            # revise editable-field set did not grow this session).
            new.categories.set(old.categories.all())
            old.deleted_at = timezone.now()
            old.replaced_by = new
            old.save(update_fields=["deleted_at", "replaced_by", "updated_at"])
            old.reports.filter(status=ReportStatus.OPEN).update(status=ReportStatus.RESOLVED)
        new.usage_count = 0  # brand-new row; skip the mixin's fallback count
        return Response(self.get_serializer(new).data, status=status.HTTP_201_CREATED)


class ModerationFlagsView(APIView):
    """GET /api/moderation/flags/ — §K2's "Flagged" tab: every question with
    at least one OPEN report, oldest report first, with reviewer context,
    §J1 usage count and the open reports themselves. Unpaginated on purpose:
    open flags are a worklist that should stay near-empty."""

    permission_classes = (IsAdminUser,)

    def get(self, request):
        open_reports = QuestionReport.objects.filter(status=ReportStatus.OPEN)
        questions = (
            # §I: deleted questions never appear here. Delete/revise resolve
            # open flags themselves, so this filter is belt-and-suspenders
            # (e.g. rows deleted by hand in /admin).
            Question.objects.filter(reports__status=ReportStatus.OPEN, deleted_at__isnull=True)
            .distinct()
            .select_related("owner")
            .prefetch_related("categories")  # §F4: M2M feeds category_names
            .annotate(usage_count=Count("boardcell", distinct=True))
            .prefetch_related(
                Prefetch("reports", queryset=open_reports.select_related("reporter"), to_attr="open_reports")
            )
        )
        rows = sorted(questions, key=lambda q: min(r.created_at for r in q.open_reports))
        return Response({"results": FlaggedQuestionSerializer(rows, many=True).data})


class ModerationFlagResolveView(APIView):
    """POST /api/moderation/flags/<question_id>/resolve/ (§K2).

    {"action": "dismiss"}: keep the question, mark its open reports resolved.
    {"action": "reject", "note": "..."}: apply the SAME field semantics as
    the pending queue's reject (moderation_status=rejected + moderation_note;
    note required) and resolve the reports. What rejection actually does to
    an approved question (documented, not invented): it stops being publicly
    visible, so it disappears from public category listings and future game
    builds; games already containing it keep their copy (cells hold a PROTECT
    FK and snapshots serialize the text regardless of status). The queue's
    own approve/reject endpoints stay pending-only — this is a separate
    inflow, not a reshape of those mechanics."""

    permission_classes = (IsAdminUser,)

    def post(self, request, question_id):
        question = Question.objects.filter(pk=question_id).first()
        if question is None:
            return Response({"detail": "No such question."}, status=status.HTTP_404_NOT_FOUND)
        open_reports = question.reports.filter(status=ReportStatus.OPEN)
        if not open_reports.exists():
            return Response(
                {"detail": "No open reports on this question — someone may have resolved them already."},
                status=status.HTTP_409_CONFLICT,
            )
        outcome = str(request.data.get("action") or "").strip()
        if outcome == "reject":
            note = str(request.data.get("note") or "").strip()
            if not note:
                return Response({"note": ["A rejection note is required."]}, status=status.HTTP_400_BAD_REQUEST)
            question.moderation_status = ModerationStatus.REJECTED
            question.moderation_note = note
            question.save(update_fields=["moderation_status", "moderation_note", "updated_at"])
            # §K3: flag-resolve reject goes through the same notification
            # helper as the queue's reject (skip rules + fail-silent inside).
            send_moderation_outcome_email(question, approved=False, actor=request.user)
        elif outcome != "dismiss":
            return Response({"action": ["Must be 'dismiss' or 'reject'."]}, status=status.HTTP_400_BAD_REQUEST)
        open_reports.update(status=ReportStatus.RESOLVED)
        return Response({"question_id": question.id, "action": outcome, "moderation_status": question.moderation_status})


class ModerationCountsView(APIView):
    """Pending counts for the nav badge."""

    permission_classes = (IsAdminUser,)

    def get(self, request):
        pending = ModerationStatus.PENDING
        return Response(
            {
                # §F5: the badge must match the (deleted-filtered) queue list —
                # same rule questions got in #9.
                "categories": Category.objects.filter(
                    moderation_status=pending, deleted_at__isnull=True
                ).count(),
                # §I: the badge must match the (deleted-filtered) queue list.
                "questions": Question.objects.filter(
                    moderation_status=pending, deleted_at__isnull=True
                ).count(),
            }
        )
