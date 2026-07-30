"""§I2 (Handoff #10): per-IP throttles for the public auth surface.

Applied PER-VIEW (login / register / password forgot / password reset), NOT
as a DRF global default — game join and the polling snapshot must stay
unthrottled: a bar full of phones behind one NAT IP is the normal case, not
an attack. Rates live in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].

The proxy problem, handled explicitly: behind Caddy, REMOTE_ADDR is the
proxy, so throttling on it would count the WHOLE SITE as one client. When
settings.DJANGO_BEHIND_PROXY is on (the same env flag that gates
SECURE_PROXY_SSL_HEADER), the client is read from X-Forwarded-For instead —
equivalent to DRF's NUM_PROXIES = 1: with exactly one trusted proxy, the
LAST XFF entry is the address that proxy saw, i.e. the real client (earlier
entries are client-supplied and spoofable). Caddy's default reverse_proxy
sets X-Forwarded-For; the owner should confirm theirs does (CHANGES.md ⚠️).
The flag is read at CALL time so tests can override_settings it.

429 bodies are DRF's default {"detail": ...} — a NEW status on these
endpoints, not a mutated shape (C4). The §K1 forgot-password 60s per-account
cooldown is UNCHANGED and complementary: this is per-IP; neither may alter
response bodies (enumeration, pinned).
"""
from django.conf import settings
from rest_framework.throttling import AnonRateThrottle


class ProxyAwareAnonThrottle(AnonRateThrottle):
    """AnonRateThrottle whose client ident survives a reverse proxy."""

    def get_ident(self, request):
        if getattr(settings, "DJANGO_BEHIND_PROXY", False):
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
            if forwarded:
                # NUM_PROXIES = 1 semantics: one trusted hop appended the
                # last entry, which is therefore the real client address.
                return forwarded.split(",")[-1].strip()
        return request.META.get("REMOTE_ADDR")


# One subclass per scope: DRF's cache key is throttle_<scope>_<ident>, so the
# scopes keep the endpoints' counters from colliding (pinned by test).


class LoginRateThrottle(ProxyAwareAnonThrottle):
    scope = "login"


class RegisterRateThrottle(ProxyAwareAnonThrottle):
    scope = "register"


class PasswordForgotRateThrottle(ProxyAwareAnonThrottle):
    scope = "password_forgot"


class PasswordResetRateThrottle(ProxyAwareAnonThrottle):
    scope = "password_reset"
