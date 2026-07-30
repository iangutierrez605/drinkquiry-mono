from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from .health import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    # §I3 (Handoff #10): the probe endpoint — a static path registered before
    # every include; its /api/health/ prefix can't be swallowed by
    # games/<code>/ (different prefix — verified, per the section's warning).
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/auth/", include("accounts.urls")),
    # §J2 (Handoff #9): staff user management, in the /api/moderation/
    # namespace but housed with accounts. Registered BEFORE trivia's include
    # (which owns the rest of /api/moderation/ via its router) — static
    # paths before parameterized ones, per the house rule.
    path("api/moderation/users/", include("accounts.admin_urls")),
    path("api/", include("trivia.urls")),
    path("api/", include("games.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
