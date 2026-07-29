from django.urls import path
from rest_framework.routers import DefaultRouter

from .moderation import ModerationCategoryViewSet, ModerationCountsView, ModerationQuestionViewSet
from .views import CategoryViewSet, QuestionViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("questions", QuestionViewSet, basename="question")
router.register("moderation/categories", ModerationCategoryViewSet, basename="moderation-category")
router.register("moderation/questions", ModerationQuestionViewSet, basename="moderation-question")

urlpatterns = [
    path("moderation/counts/", ModerationCountsView.as_view(), name="moderation-counts"),
] + router.urls
