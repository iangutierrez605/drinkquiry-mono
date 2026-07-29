from django.urls import path
from rest_framework.routers import DefaultRouter

from .moderation import (
    ModerationCategoryViewSet,
    ModerationCountsView,
    ModerationFlagResolveView,
    ModerationFlagsView,
    ModerationQuestionViewSet,
)
from .views import CategoryViewSet, QuestionViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("questions", QuestionViewSet, basename="question")
router.register("moderation/categories", ModerationCategoryViewSet, basename="moderation-category")
router.register("moderation/questions", ModerationQuestionViewSet, basename="moderation-question")

urlpatterns = [
    path("moderation/counts/", ModerationCountsView.as_view(), name="moderation-counts"),
    # Handoff #8 §K2: the host-flag inflow's staff side. Static paths before
    # the router so nothing gets swallowed by viewset lookups.
    path("moderation/flags/", ModerationFlagsView.as_view(), name="moderation-flags"),
    path(
        "moderation/flags/<int:question_id>/resolve/",
        ModerationFlagResolveView.as_view(),
        name="moderation-flag-resolve",
    ),
] + router.urls
