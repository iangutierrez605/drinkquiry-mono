"""§F2 (Handoff #12): Cloudflare Turnstile server-side verification.

Env-driven and OFF by default, following the Resend pattern exactly:
`TURNSTILE_SECRET_KEY` (backend verify) + `TURNSTILE_SITE_KEY` (surfaced to
the SPA at build time as VITE_TURNSTILE_SITE_KEY) both default to "".
The feature is ON iff the SECRET is set — dev, the suite and the smoke all
run keyless with the feature entirely absent.

Scope is register ONLY (v1). Login keeps its throttle as its defense
(no friction for returning users); forgot-password keeps its throttle +
per-account cooldown (a challenge there hurts locked-out humans most).
Extension points are noted in CHANGES.md; email verification is the NEXT
escalation after this and stays punted (§M).

Failure policy — decided and documented: FAIL CLOSED for register. A
verify-endpoint outage or timeout reads as "not verified" → the pinned 400.
A signup can simply retry; a bot flood slipping through during an outage
cannot be un-registered. Settings are read at CALL time so tests can
override_settings the secret and mock `requests.post` (F4).
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
VERIFY_TIMEOUT = 5  # seconds — a hung verify must not hang registration


def turnstile_enabled() -> bool:
    return bool(getattr(settings, "TURNSTILE_SECRET_KEY", ""))


def verify_turnstile(token: str, remoteip: str | None = None) -> bool:
    """POST the token to Cloudflare's siteverify. True iff verified.

    Any failure mode — missing/empty token, verify says no, network error,
    timeout, unparseable response — returns False (fail closed, see module
    docstring). Never raises.
    """
    if not token:
        return False
    payload = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remoteip:
        payload["remoteip"] = remoteip
    try:
        res = requests.post(VERIFY_URL, data=payload, timeout=VERIFY_TIMEOUT)
        return bool(res.json().get("success"))
    except Exception as exc:  # requests errors, JSON errors — all read as "no"
        logger.warning("Turnstile verify failed closed: %s", exc)
        return False
