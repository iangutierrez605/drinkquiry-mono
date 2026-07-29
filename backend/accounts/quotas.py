"""Plan quota logic — the one place that counts usage against settings.PLAN_LIMITS.

Limits are per-plan in settings (no per-user rows), so pricing changes are a
settings edit, not a migration. `None` always means unlimited.

Views call `quota_denial(user, kind)` before creating anything quota-bearing;
a non-None return is the structured 403 body the frontend understands:
    {"detail": "...", "code": "quota_<kind>", "used": n, "limit": m}
(Returned via `Response(payload, 403)` rather than `raise PermissionDenied(payload)`
because DRF's exception machinery coerces ints/None in detail dicts to strings,
which would break the documented API shape.)
"""
from django.apps import apps
from django.conf import settings
from django.utils import timezone

# kind -> (quota code suffix, human noun)
KINDS = {
    "games": "games this month",
    "categories": "custom categories",
    "questions": "custom questions",
}


def limits_for(user) -> dict:
    """Quota dict for the user's *effective* plan (expired paid == free)."""
    plans = settings.PLAN_LIMITS
    return plans.get(user.effective_plan, plans["free"])


def _mb(n: int) -> str:
    """Human MB for storage messages (payload numbers stay raw bytes)."""
    return f"{n / (1024 * 1024):g} MB"


def month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def games_used_this_month(user) -> int:
    Game = apps.get_model("games", "Game")
    return Game.objects.filter(host=user, created_at__gte=month_start()).count()


def categories_used(user) -> int:
    Category = apps.get_model("trivia", "Category")
    return Category.objects.filter(owner=user).count()


def questions_used(user) -> int:
    Question = apps.get_model("trivia", "Question")
    return Question.objects.filter(owner=user).count()


def storage_bytes_used(user) -> int:
    """§F3: summed from the persisted media_bytes/photo_bytes columns —
    maintained at save time by the models, so this never lists a bucket.
    Deleting a row frees its quota because the columns go with it."""
    from django.db.models import Sum

    Question = apps.get_model("trivia", "Question")
    Category = apps.get_model("trivia", "Category")
    q = Question.objects.filter(owner=user).aggregate(total=Sum("media_bytes"))["total"] or 0
    c = Category.objects.filter(owner=user).aggregate(total=Sum("photo_bytes"))["total"] or 0
    return q + c


_COUNTERS = {
    "games": games_used_this_month,
    "categories": categories_used,
    "questions": questions_used,
}
_LIMIT_KEYS = {"games": "games_per_month", "categories": "categories", "questions": "questions"}


def _storage_limit(user):
    # .get so PLAN_LIMITS overrides that predate §F3 read as unlimited.
    return limits_for(user).get("storage_bytes")


def usage(user) -> dict:
    """The `usage` block for GET /api/auth/profile/ (D3; storage added §F3)."""
    limits = limits_for(user)
    return {
        "games_this_month": {"used": games_used_this_month(user), "limit": limits["games_per_month"]},
        "categories": {"used": categories_used(user), "limit": limits["categories"]},
        "questions": {"used": questions_used(user), "limit": limits["questions"]},
        "storage": {"used": storage_bytes_used(user), "limit": limits.get("storage_bytes")},
    }


def _denial(user, kind: str, count: int) -> dict | None:
    """None if the user may create `count` more of `kind`; else the 403 body."""
    limit = limits_for(user)[_LIMIT_KEYS[kind]]
    if limit is None:  # unlimited
        return None
    used = _COUNTERS[kind](user)
    if used + count <= limit:
        return None
    noun = KINDS[kind]
    if limit == 0:
        detail = f"Your plan doesn't include {noun.replace(' this month', '')}. Upgrade to a creator account to unlock this."
    elif count > 1:
        detail = f"This batch of {count} would put you past your plan's limit of {limit} {noun} ({used} used)."
    else:
        detail = f"You've reached your plan's limit of {limit} {noun} ({used} used)."
    return {"detail": detail, "code": f"quota_{kind}", "used": used, "limit": limit}


def quota_denial(user, kind: str) -> dict | None:
    """None if the user may create one more `kind`; else the 403 payload.

    The payload shape here is a documented contract (no extra keys) — batch
    callers use `batch_quota_denial`, which adds `requested`.
    """
    return _denial(user, kind, 1)


def batch_quota_denial(user, kind: str, count: int) -> dict | None:
    """Batch-aware check for bulk creation (Handoff #4 §G2).

    Denies when `used + count > limit`; the payload is the standard structured
    403 plus `"requested": count` so the frontend can say "this file has 60
    questions but your plan has 40 left".
    """
    denial = _denial(user, kind, count)
    if denial is not None:
        denial["requested"] = count
    return denial


def _storage_denial(user, incoming_bytes: int) -> dict | None:
    """§F3 core check. Payload numbers are raw BYTES in the exact documented
    shape ({detail, code, used, limit}); the human text formats MB. Uses the
    incoming files' pre-resize sizes — a conservative upper bound, since the
    stored (post-resize) bytes can only be smaller."""
    limit = _storage_limit(user)
    if limit is None or incoming_bytes <= 0:
        return None
    used = storage_bytes_used(user)
    if used + incoming_bytes <= limit:
        return None
    if limit == 0:
        detail = "Your plan doesn't include media storage. Upgrade to a creator account to unlock this."
    else:
        detail = (
            f"This upload adds {_mb(incoming_bytes)} of media but you've used "
            f"{_mb(used)} of your plan's {_mb(limit)} storage. Delete some media first."
        )
    return {"detail": detail, "code": "quota_storage", "used": used, "limit": limit}


def storage_quota_denial(user, incoming_bytes: int) -> dict | None:
    """Single-upload storage check (§F3) — same no-extra-keys contract as
    quota_denial: {"detail", "code": "quota_storage", "used", "limit"}."""
    return _storage_denial(user, incoming_bytes)


def batch_storage_quota_denial(user, incoming_bytes: int) -> dict | None:
    """Bulk-zip storage check (§F3): the standard payload plus
    `"requested": <total bytes of the incoming batch>`."""
    denial = _storage_denial(user, incoming_bytes)
    if denial is not None:
        denial["requested"] = incoming_bytes
    return denial
