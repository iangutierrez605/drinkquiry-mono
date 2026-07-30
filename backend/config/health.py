"""§I3 (Handoff #10): GET /api/health/ — the load balancer / compose probe.

AllowAny, unthrottled, no auth (no authentication_classes at all, so a stale
Authorization header can never turn a probe into a 401). Healthy:
{"status": "ok"} with a 200 — the EXACT body the smoke test pins — after a
trivial DB SELECT 1 and, when REDIS_URL is configured, a cache set/get probe.
Any failing dependency: 503 with {"status": "degraded", <component>: <why>}.

Routed under /api/health/ in config/urls.py — a static path registered
before the app includes, and on a different prefix from games/<code>/ so the
code lookup can't swallow it (checked, per the section's own warning).
"""
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)
    throttle_classes = ()

    def get(self, request):
        problems = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 — a health check reports, never raises
            problems["database"] = str(exc)[:200]
        if settings.REDIS_URL:
            # Only probed when Redis is actually configured: LocMem can't
            # meaningfully fail, and dev shouldn't report degraded for a
            # dependency it doesn't have.
            try:
                cache.set("healthcheck:probe", "ok", 5)
                if cache.get("healthcheck:probe") != "ok":
                    problems["cache"] = "probe write/read mismatch"
            except Exception as exc:  # noqa: BLE001
                problems["cache"] = str(exc)[:200]
        if problems:
            return Response({"status": "degraded", **problems}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": "ok"})
