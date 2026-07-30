from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
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
