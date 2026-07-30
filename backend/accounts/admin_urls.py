"""Routes for accounts/admin_api.py (Handoff #9 §J2), mounted at
/api/moderation/users/ from config/urls.py — BEFORE trivia's include, so the
static staff-namespace path is matched ahead of any router lookups (house
rule: static paths before parameterized ones)."""
from django.urls import path

from .admin_api import AdminUserDetailView, AdminUserListView

urlpatterns = [
    path("", AdminUserListView.as_view(), name="moderation-users"),
    path("<int:pk>/", AdminUserDetailView.as_view(), name="moderation-user-detail"),
]
