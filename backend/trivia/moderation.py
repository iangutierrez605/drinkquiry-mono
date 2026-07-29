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

Approving a category does NOT touch its questions — every item is vetted
separately. Double-acting reviewers get a 409 (no locking; last write would
win otherwise, and a plain status check is enough for a two-person team).
§K deliberately does not reshape those approve/reject mechanics — the flag
flow resolves through its own endpoint, applying the same fields.
"""
from django.db.models import Count, Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Category, ModerationStatus, Question, QuestionReport, ReportStatus
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
        if wanted not in VALID_STATUSES:
            # Raised as a DRF ValidationError → clean 400 with the valid choices.
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"status": f"Unknown status. One of: {', '.join(sorted(VALID_STATUSES))}."})
        return qs.filter(moderation_status=wanted)

    def _act(self, request, *, approve: bool):
        obj = self.get_object()
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
        return super().get_queryset().annotate(usage_count=Count("boardcolumn", distinct=True))


class ModerationQuestionViewSet(_ModerationViewSet):
    model = Question
    serializer_class = ModerationQuestionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        # §J1: usage = cells referencing the question, across all games.
        return qs.select_related("category").annotate(usage_count=Count("boardcell", distinct=True))

    @action(detail=True, methods=["get"])
    def similar(self, request, pk=None):
        """§K1: nearest existing approved matches for this question — a
        sub-endpoint rather than a `similar` block on the list serializer so
        the queue page stays snappy (the frontend fetches per card). A review
        AID, not auto-rejection; works for any status so the Flagged tab can
        reuse it on approved questions."""
        return Response({"similar": similar_questions(self.get_object())})


class ModerationFlagsView(APIView):
    """GET /api/moderation/flags/ — §K2's "Flagged" tab: every question with
    at least one OPEN report, oldest report first, with reviewer context,
    §J1 usage count and the open reports themselves. Unpaginated on purpose:
    open flags are a worklist that should stay near-empty."""

    permission_classes = (IsAdminUser,)

    def get(self, request):
        open_reports = QuestionReport.objects.filter(status=ReportStatus.OPEN)
        questions = (
            Question.objects.filter(reports__status=ReportStatus.OPEN)
            .distinct()
            .select_related("category", "owner")
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
                "categories": Category.objects.filter(moderation_status=pending).count(),
                "questions": Question.objects.filter(moderation_status=pending).count(),
            }
        )
