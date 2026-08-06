"""§F2/§F4/§F6 (Handoff #18): derived entitlement state — the one module
that answers "what does this user's billing let them do right now".

Import rules: this module reaches other apps' models via apps.get_model
(the accounts/quotas.py pattern) so billing stays import-cycle-free —
accounts/quotas, trivia/views and games/services all import from here.

Scope model (ruling, flagged in CHANGES): a question belongs to exactly ONE
scope — a single pack entitlement (every category it sits in is bound to
that entitlement) or the account (every category unbound). Mixing scopes on
one question, or moving a question between scopes via PATCH, is rejected in
the serializer — that one rule is what keeps the pack budget, the account
quota and the storage split all exact (and closes the recategorize-out-of-
the-pack quota bypass).
"""
from django.apps import apps
from django.conf import settings
from django.db.models import Q, Sum

from .catalog import ACCOUNT_GRANTS, VENUE_ACTIVE_QUESTION_LIMIT
from .models import BOUND_KINDS, VENUE_KINDS, Entitlement, EntitlementKind


def user_entitlements(user):
    """All of a user's entitlements with sources preloaded (is_active reads
    source_subscription). Anonymous users have none."""
    if not getattr(user, "is_authenticated", False):
        return Entitlement.objects.none()
    return Entitlement.objects.filter(user=user).select_related("source_subscription")


def active_entitlements(user, kinds=None):
    rows = [e for e in user_entitlements(user) if e.is_active]
    if kinds is not None:
        rows = [e for e in rows if e.kind in kinds]
    return rows


def has_active_entitlement(user, kinds=None) -> bool:
    return bool(active_entitlements(user, kinds))


def account_grants(user) -> dict:
    """§F2 union input: the account-scoped allowances contributed by ACTIVE
    venue-kind entitlements (party/big/tournament packs contribute nothing
    account-scoped — their world is their bound categories)."""
    merged: dict = {}
    for ent in active_entitlements(user, VENUE_KINDS):
        for key, value in ACCOUNT_GRANTS.get(ent.kind, {}).items():
            if key in merged and merged[key] is None:
                continue  # already unlimited
            if value is None:
                merged[key] = None
            elif key not in merged:
                merged[key] = value
            else:
                merged[key] = max(merged[key], value)
    return merged


def venue_active(user) -> bool:
    return has_active_entitlement(user, VENUE_KINDS)


def unconsumed_active_passes(user):
    """§F8 slice: tournament passes that are active and not yet bound to a
    tournament — each permits exactly one tournament create."""
    return [
        e
        for e in active_entitlements(user, (EntitlementKind.TOURNAMENT_PASS,))
        if e.tournament_id is None
    ]


# --- counters ---------------------------------------------------------------


def _questions(user=None):
    return apps.get_model("trivia", "Question").objects.filter(deleted_at__isnull=True)


def active_questions_used(user) -> int:
    """§F6: the venue counter — live, NON-archived, account-scope (unbound)
    questions. Archived rows are the reversible shelf; pack-bound rows
    answer to their pack."""
    return (
        _questions()
        .filter(owner=user, is_archived=False)
        .exclude(categories__entitlement__isnull=False)
        .distinct()
        .count()
    )


def pack_questions_used(entitlement) -> int:
    """§F4: live questions across the entitlement's bound categories —
    the pack budget counter (archived rows still occupy pack budget: they
    are stored pack content, just shelved)."""
    return (
        _questions()
        .filter(categories__entitlement=entitlement)
        .distinct()
        .count()
    )


def pack_categories_used(entitlement) -> int:
    Category = apps.get_model("trivia", "Category")
    return Category.objects.filter(entitlement=entitlement, deleted_at__isnull=True).count()


def pack_storage_used(entitlement) -> int:
    Category = apps.get_model("trivia", "Category")
    q = (
        _questions()
        .filter(categories__entitlement=entitlement)
        .distinct()
        .aggregate(total=Sum("media_bytes"))["total"]
        or 0
    )
    c = (
        Category.objects.filter(entitlement=entitlement, deleted_at__isnull=True).aggregate(
            total=Sum("photo_bytes")
        )["total"]
        or 0
    )
    return q + c


# --- structured denials (house quota shape) ---------------------------------

# kind of pack → the quota code its question budget denies with. The
# tournament pass uses the handoff's quota_tournament_questions; the two
# game packs share quota_pack_questions.
PACK_QUESTION_CODES = {
    EntitlementKind.PARTY_PACK: "quota_pack_questions",
    EntitlementKind.BIG_PACK: "quota_pack_questions",
    EntitlementKind.TOURNAMENT_PASS: "quota_tournament_questions",
}


def pack_question_denial(entitlement, count: int = 1) -> dict | None:
    """House-shaped 403 body when `count` more questions would bust the
    pack's budget, else None. Shape: {detail, code, used, limit}
    (+`requested` added by batch callers, the batch_quota_denial pattern)."""
    limit = entitlement.question_limit
    if limit is None:
        return None
    used = pack_questions_used(entitlement)
    if used + count <= limit:
        return None
    code = PACK_QUESTION_CODES[entitlement.kind]
    if count > 1:
        detail = (
            f"This batch of {count} would put this pack past its {limit}-question "
            f"budget ({used} used)."
        )
    else:
        detail = f"This pack's {limit}-question budget is full ({used} used)."
    return {"detail": detail, "code": code, "used": used, "limit": limit}


def pack_category_denial(entitlement) -> dict | None:
    """§F4: bound-category count is small-capped (catalog category_limit,
    default 5 — flagged). Same house shape, code quota_pack_categories."""
    from .catalog import PRODUCTS

    limit = None
    for entry in PRODUCTS.values():
        if entry["kind"] == entitlement.kind and not entry.get("reactivation_of"):
            limit = entry.get("category_limit")
            break
    if limit is None:
        return None
    used = pack_categories_used(entitlement)
    if used + 1 <= limit:
        return None
    return {
        "detail": f"This pack already has its {limit} categories ({used} used).",
        "code": "quota_pack_categories",
        "used": used,
        "limit": limit,
    }


def pack_storage_denial(entitlement, incoming_bytes: int) -> dict | None:
    """§F4: media inside bound categories draws on the per-pack storage
    allowance (catalog storage_bytes; defaults flagged) through the same
    conservative pre-resize accounting the account check uses."""
    from .catalog import PRODUCTS

    limit = None
    for entry in PRODUCTS.values():
        if entry["kind"] == entitlement.kind and not entry.get("reactivation_of"):
            limit = entry.get("storage_bytes")
            break
    if limit is None or incoming_bytes <= 0:
        return None
    used = pack_storage_used(entitlement)
    if used + incoming_bytes <= limit:
        return None
    mb = lambda n: f"{n / (1024 * 1024):g} MB"  # noqa: E731 — the quotas helper's format
    return {
        "detail": (
            f"This upload adds {mb(incoming_bytes)} of media but this pack has used "
            f"{mb(used)} of its {mb(limit)} storage."
        ),
        "code": "quota_pack_storage",
        "used": used,
        "limit": limit,
    }


def active_question_denial(user, count: int = 1) -> dict | None:
    """§F6: the 100-ACTIVE choke, house-shaped (code quota_active_questions).
    Callers apply this only when the plain account lane did NOT already
    permit the write (union = most permissive lane wins)."""
    limit = VENUE_ACTIVE_QUESTION_LIMIT if venue_active(user) else 0
    used = active_questions_used(user)
    if used + count <= limit:
        return None
    if limit == 0:
        detail = "Your plan doesn't include active custom questions right now."
    elif count > 1:
        detail = (
            f"This batch of {count} would put you past {limit} active questions "
            f"({used} active). Archive some first."
        )
    else:
        detail = (
            f"You already have {used} of {limit} active questions. Archive one "
            "to make room — archived questions keep forever and can come back."
        )
    return {"detail": detail, "code": "quota_active_questions", "used": used, "limit": limit}


def entitlement_usage_summary(user) -> list[dict]:
    """§F2/§F7: the profile `usage.entitlements` block AND the /status/
    entitlements list share this shape (pinned by tests):
    {id, kind, is_active, active_from, active_until, question_limit,
     questions_used, game_limit, active_questions?} — active_questions only
    on venue kinds."""
    rows = []
    for ent in user_entitlements(user).order_by("-created_at", "-id"):
        row = {
            "id": ent.id,
            "kind": ent.kind,
            "is_active": ent.is_active,
            "active_from": ent.active_from.isoformat() if ent.active_from else None,
            "active_until": ent.active_until.isoformat() if ent.active_until else None,
            "question_limit": ent.question_limit,
            "questions_used": pack_questions_used(ent) if ent.kind in BOUND_KINDS else None,
            "game_limit": ent.game_limit,
        }
        if ent.kind in VENUE_KINDS:
            row["active_questions"] = {
                "used": active_questions_used(user),
                "limit": VENUE_ACTIVE_QUESTION_LIMIT,
            }
        rows.append(row)
    return rows


# --- gates ------------------------------------------------------------------
# §F4 hosting/editing gate for bound content whose pack lapsed, and the
# §F6/§F7 authoring-rights hosting gate — both new documented 403 shapes
# (exactly {detail, code}; pinned by tests).
PACK_INACTIVE = {
    "detail": (
        "This pack's 30-day window has ended — its questions are kept safe but "
        "read-only. Reactivate the pack to host or edit them again."
    ),
    "code": "pack_inactive",
}

PLAN_REQUIRED = {
    "detail": (
        "Hosting boards with your own custom categories needs an active plan or "
        "pack — the free library is always available."
    ),
    "code": "plan_required",
}


def entitlement_for_categories(categories):
    """Resolve the ONE entitlement a category set is bound to.

    Returns (entitlement_id | None, ok). ok=False when the set mixes scopes
    (bound + unbound, or two different entitlements) — the serializer turns
    that into a 400."""
    ids = {c.entitlement_id for c in categories}
    if ids == {None}:
        return None, True
    if None in ids or len(ids) != 1:
        return None, False
    return next(iter(ids)), True


def can_paid_write(user) -> bool:
    """The IsCreator extension: paid-plan writes (PATCH/DELETE on own
    content, archive actions, hand-picking) are for manual creators OR any
    account holding an ACTIVE entitlement of any kind (C-3 default)."""
    return user.is_creator or has_active_entitlement(user)


def owns_any_entitlement(user) -> bool:
    """Permission-layer key: has this account EVER bought (active or
    lapsed)? Lapsed buyers pass the coarse permission gate so the views can
    answer with the informative pack_inactive instead of a generic 403;
    their UNBOUND writes re-deny in the view via can_paid_write."""
    if not getattr(user, "is_authenticated", False):
        return False
    return Entitlement.objects.filter(user=user).exists()


def can_host_own_custom(user) -> bool:
    """§F6/§F7 ruling (flagged): hosting your OWN unbound custom categories
    requires the same authoring rights that let you make them — a nonzero
    categories allowance from plan, overrides or an active venue-kind
    entitlement. A lapsed venue (and, behavior change, a lapsed manual
    creator without overrides) is gated; the free library never is."""
    from accounts.quotas import limits_for

    limit = limits_for(user).get("categories")
    return limit is None or limit > 0
