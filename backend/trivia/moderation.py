"""Staff-only moderation review API (Handoff #4 §F1).

Endpoints (all `IsAdminUser` — the frontend's is_staff gate is cosmetic):

  GET  /api/moderation/categories/            paginated, ?status= (default pending),
  GET  /api/moderation/questions/             oldest-first by created_at
  POST /api/moderation/{kind}/{id}/approve/   PENDING only (else 409); clears note
  POST /api/moderation/{kind}/{id}/reject/    PENDING only; requires {"note": "..."}
  GET  /api/moderation/counts/                {"categories": n, "questions": n} pending

Approving a category does NOT touch its questions — every item is vetted
separately. Double-acting reviewers get a 409 (no locking; last write would
win otherwise, and a plain status check is enough for a two-person team).
"""
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Category, ModerationStatus, Question
from .serializers import ModerationCategorySerializer, ModerationQuestionSerializer

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


class ModerationQuestionViewSet(_ModerationViewSet):
    model = Question
    serializer_class = ModerationQuestionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related("category")


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
