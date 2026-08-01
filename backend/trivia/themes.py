"""§G (Handoff #10): themes — the tag table's two API surfaces.

Host-facing:
  GET /api/themes/            any authenticated user (the create screen is
                              Knox-gated anyway). Active themes, ordered by
                              name, UNPAGINATED (staff-curated scale), each:
                              {id, name, description,
                               categories: [{id, name, usable_question_count}]}
                              — categories filtered to ones THIS user can see
                              (same visibility rule as /api/categories/,
                              deleted excluded per §F5), counts computed per
                              user exactly like CategorySerializer does.

Staff (all IsAdminUser; the frontend's is_staff gate is cosmetic):
  GET/POST   /api/moderation/themes/         list active / create
  PATCH      /api/moderation/themes/<id>/    name / description / category ids
  DELETE     /api/moderation/themes/<id>/    SOFT delete, 204; 409 on
                                             double-delete (house flavor).
                                             Name reuse after delete is
                                             permitted by the partial unique
                                             constraint.

Game creation is UNCHANGED — themes never reach games/services (deliberate;
see create_game's docstring). Creator-owned themes, theme moderation and
theme restore are §M.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, permissions, serializers, status, viewsets
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Category, ModerationStatus, Theme, Visibility
from .views import search_param

PUBLIC_APPROVED = Q(visibility=Visibility.PUBLIC, moderation_status=ModerationStatus.APPROVED)


def visible_categories_for(user):
    """Active categories this user can see — the /api/categories/ rule."""
    qs = Category.objects.filter(deleted_at__isnull=True)
    if user.is_authenticated:
        return qs.filter(PUBLIC_APPROVED | Q(owner=user) | Q(owner__isnull=True)).distinct()
    return qs.filter(PUBLIC_APPROVED | Q(owner__isnull=True))


class ThemeListView(APIView):
    """The host create screen's theme strip data.

    §F1 (Handoff #15): `?search=` — icontains on name OR description
    (shared search_param semantics). ADDITIVE: the unfiltered response and
    its per-theme key set are pinned by the smoke — a BARE ARRAY, no page
    envelope, because themes are staff-curated (dozens, not thousands).
    Pagination trigger: if staff curation ever grows this list past a few
    hundred (≈ when the strip's search stops being enough and the payload's
    per-user category counts start to hurt), page it like the library —
    until then the array stays flat and the frontend renders/slices it."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        visible_ids = set(visible_categories_for(request.user).values_list("id", flat=True))
        themes = Theme.objects.filter(deleted_at__isnull=True).prefetch_related("categories").order_by("name", "id")
        if search := search_param(request):
            themes = themes.filter(Q(name__icontains=search) | Q(description__icontains=search))
        payload = []
        for theme in themes:
            categories = [
                {
                    "id": c.id,
                    "name": c.name,
                    # Per-user count, exactly like CategorySerializer.
                    "usable_question_count": c.usable_question_count(request.user),
                }
                for c in theme.categories.all()
                if c.id in visible_ids
            ]
            payload.append(
                {
                    "id": theme.id,
                    "name": theme.name,
                    "description": theme.description,
                    "categories": sorted(categories, key=lambda c: c["name"].lower()),
                }
            )
        return Response(payload)


class ModerationThemeSerializer(serializers.ModelSerializer):
    categories = serializers.PrimaryKeyRelatedField(
        many=True, required=False, queryset=Category.objects.filter(deleted_at__isnull=True)
    )
    category_names = serializers.SerializerMethodField()
    # §F7 (Handoff #15): {id, name} pairs for the ACTIVE members — the edit
    # form's searchable picker needs id→name seeds for its chips, and the
    # two existing arrays can't be zipped (`categories` ids include deleted
    # rows and follow M2M order; `category_names` is active-only and
    # name-sorted). Read-only, additive; writes still use `categories` ids.
    category_details = serializers.SerializerMethodField()

    class Meta:
        model = Theme
        fields = ("id", "name", "description", "categories", "category_names", "category_details", "created_at", "deleted_at")
        read_only_fields = ("created_at", "deleted_at")

    def get_category_names(self, obj):
        # §F5: a category deleted after being grouped stays in the M2M row
        # but is hidden everywhere active — the staff list hides it too.
        return sorted(c.name for c in obj.categories.all() if c.deleted_at is None)

    def get_category_details(self, obj):
        # Same active-only membership as category_names, same name sort.
        return sorted(
            ({"id": c.id, "name": c.name} for c in obj.categories.all() if c.deleted_at is None),
            key=lambda c: c["name"],
        )

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A theme name is required.")
        clash = Theme.objects.filter(name=value, deleted_at__isnull=True)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("An active theme already has this name.")
        return value


class ModerationThemeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    serializer_class = ModerationThemeSerializer
    # Staff-curated scale; the tab renders the lot as a BARE ARRAY (no page
    # envelope — the frontend must not run this through a page helper).
    # §F1 (#15) pagination trigger: page it like the library the day staff
    # curation outgrows one screen (a few hundred themes); until then
    # ?search= below is the scale valve.
    pagination_class = None

    def get_queryset(self):
        qs = Theme.objects.prefetch_related("categories").order_by("name", "id")
        if self.action == "list":
            qs = qs.filter(deleted_at__isnull=True)
            # §F1 (#15): list-only ?search= — icontains on name OR
            # description, shared search_param semantics (strip, cap 100).
            if search := search_param(self.request):
                qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
            return qs
        # Detail/actions see deleted themes so a double-delete race gets the
        # house 409, not a 404.
        return qs

    def destroy(self, request, *args, **kwargs):
        theme = self.get_object()
        if theme.deleted_at is not None:
            return Response(
                {"detail": "Already deleted — someone may have beaten you to it."},
                status=status.HTTP_409_CONFLICT,
            )
        theme.deleted_at = timezone.now()
        theme.save(update_fields=["deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
