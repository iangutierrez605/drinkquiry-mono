"""Staff-only user management API (Handoff #9 §J).

Routes live under /api/moderation/users/ (keeping the staff namespace
uniform; wired in config/urls.py BEFORE trivia's router include — static
paths before parameterized ones, house rule).

  GET   /api/moderation/users/        paginated (25/page), ?search= (email OR
                                      display_name icontains), ?plan=,
                                      ?is_staff=, newest-joined first.
  PATCH /api/moderation/users/<id>/   accepts `plan`, `plan_expires_at`,
                                      `limit_overrides` ONLY — anything else
                                      in the body is a 400 (pinned).

Explicitly NOT editable here: is_staff, email, password. Staff-granting
stays in Django admin / shell — a compromised moderator account must not be
able to mint more staff (or lock out / impersonate users). Both endpoints
are IsAdminUser; the frontend's staff gate is cosmetic as always.

Payments truth (§J, said plainly): there is NO payment system in this
codebase — nothing to "view". `plan` is a hand-set field; "grant create
access" and "override payment for a demo" are both plan=creator (a demo adds
plan_expires_at, which lapses back to free automatically); "raise
allowances" is the §J1 per-user limit_overrides. The UI repeats this note.

Each row carries the §J1 usage block (quotas.usage) so staff can judge a
raise request. That's a few COUNT queries per user — at page_size 25 that's
fine; don't prematurely optimize (noted per the handoff).
"""
from rest_framework import generics, pagination, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser

from . import quotas
from .models import Plan, User

PATCHABLE_FIELDS = {"plan", "plan_expires_at", "limit_overrides"}


class AdminUserPagination(pagination.PageNumberPagination):
    page_size = 25


class AdminUserSerializer(serializers.ModelSerializer):
    """The §J2 row: identity + plan truth (raw plan AND effective plan, so a
    lapsed demo is visible as creator-but-effectively-free) + overrides +
    live usage."""

    effective_plan = serializers.CharField(read_only=True)
    usage = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "display_name", "plan", "effective_plan",
            "plan_expires_at", "is_staff", "date_joined", "limit_overrides", "usage",
        )
        read_only_fields = ("id", "email", "display_name", "is_staff", "date_joined")

    def get_usage(self, obj):
        return quotas.usage(obj)

    def validate_plan(self, value):
        if value not in {p.value for p in Plan}:
            raise serializers.ValidationError(f"One of: {', '.join(p.value for p in Plan)}.")
        return value

    def validate_limit_overrides(self, value):
        try:
            return quotas.validate_overrides(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))


class AdminUserListView(generics.ListAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = AdminUserSerializer
    pagination_class = AdminUserPagination

    def get_queryset(self):
        from django.db.models import Q

        qs = User.objects.order_by("-date_joined", "-id")
        params = self.request.query_params
        if search := params.get("search", "").strip():
            qs = qs.filter(Q(email__icontains=search) | Q(display_name__icontains=search))
        if plan := params.get("plan", "").strip():
            if plan not in {p.value for p in Plan}:
                raise ValidationError({"plan": f"One of: {', '.join(p.value for p in Plan)}."})
            qs = qs.filter(plan=plan)
        if (is_staff := params.get("is_staff", "").strip().lower()) in ("true", "false"):
            qs = qs.filter(is_staff=(is_staff == "true"))
        return qs


class AdminUserDetailView(generics.UpdateAPIView):
    """PATCH only (http_method_names below drops PUT — partial edits are the
    whole point). Unknown/forbidden body fields → 400 rather than silently
    ignored: "PATCH is_staff quietly did nothing" is exactly the confusion
    this guard exists to prevent (pinned by a test)."""

    permission_classes = (IsAdminUser,)
    serializer_class = AdminUserSerializer
    queryset = User.objects.all()
    http_method_names = ["patch", "options", "head"]

    def update(self, request, *args, **kwargs):
        if forbidden := sorted(set(request.data.keys()) - PATCHABLE_FIELDS):
            raise ValidationError(
                {f: ["Not editable here — only plan, plan_expires_at, limit_overrides."] for f in forbidden}
            )
        return super().update(request, *args, **kwargs)
