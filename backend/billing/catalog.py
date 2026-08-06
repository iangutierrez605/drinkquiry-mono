"""§F2 (Handoff #18): the server-side product catalog — the allowlist.

Nothing the browser sends about a product is ever trusted: /checkout/ takes
a `product` KEY, validates it here, and resolves the Stripe Price ID
server-side from settings (env). Price IDs never reach the browser; display
prices live here and are served read-only by /products/.

⚠️ OWNER CONFIRM (C-8 adjacent, flagged in CHANGES): the display prices
below are PLACEHOLDERS except Venue Tournament's $59.99 (the one price the
handoff states). The real charge amounts live on the Stripe Prices the
owner creates (§N) — these strings are marketing copy only — but they must
match before launch.
"""
from django.conf import settings

from .models import EntitlementKind

MB = 1024 * 1024

# key → catalog entry. `mode` is the Checkout mode; `interval` is display
# only; `window_days` is the purchase-sourced entitlement window;
# `reactivation_of` marks the two dark reactivation keys (they extend an
# existing entitlement instead of creating one).
PRODUCTS = {
    "party_game_50": {
        "display_name": "Party Game",
        "kind": EntitlementKind.PARTY_PACK,
        "mode": "payment",
        "price_env": "STRIPE_PRICE_PARTY_GAME_50",
        "display_price": "$9.99",
        "interval": None,
        "question_limit": 50,
        "game_limit": None,
        "window_days": 30,
        "storage_bytes": 50 * MB,
        "category_limit": 5,
        "enabled": True,
        "blurb": "One custom game night: up to 50 of your own questions, hostable for 30 days.",
    },
    "big_game_100": {
        "display_name": "Big Game",
        "kind": EntitlementKind.BIG_PACK,
        "mode": "payment",
        "price_env": "STRIPE_PRICE_BIG_GAME_100",
        "display_price": "$19.99",
        "interval": None,
        "question_limit": 100,
        "game_limit": None,
        "window_days": 30,
        "storage_bytes": 100 * MB,
        "category_limit": 5,
        "enabled": True,
        "blurb": "The full-size custom night: up to 100 questions, hostable for 30 days.",
    },
    "venue_monthly": {
        "display_name": "Venue",
        "kind": EntitlementKind.VENUE,
        "mode": "subscription",
        "price_env": "STRIPE_PRICE_VENUE_MONTHLY",
        "display_price": "$29.99",
        "interval": "month",
        "question_limit": None,  # account-scoped: the 100-ACTIVE gate governs
        "game_limit": None,
        "window_days": None,
        "storage_bytes": None,  # account-scoped grant, see ACCOUNT_GRANTS
        "category_limit": None,
        "enabled": True,
        "blurb": "For bars: rolling custom trivia, your branding on every screen, saved results.",
    },
    "tournament_pass": {
        "display_name": "Tournament Pass",
        "kind": EntitlementKind.TOURNAMENT_PASS,
        "mode": "payment",
        "price_env": "STRIPE_PRICE_TOURNAMENT_PASS",
        "display_price": "$49.99",
        "interval": None,
        "question_limit": 200,
        "game_limit": 6,
        "window_days": 30,
        "storage_bytes": 200 * MB,
        "category_limit": 5,
        "enabled": True,
        "blurb": "One full tournament: up to 6 games and 200 questions, 30 days to run it.",
    },
    "venue_tournament_monthly": {
        "display_name": "Venue Tournament",
        "kind": EntitlementKind.VENUE_TOURNAMENT,
        "mode": "subscription",
        "price_env": "STRIPE_PRICE_VENUE_TOURNAMENT_MONTHLY",
        "display_price": "$59.99",
        "interval": "month",
        "question_limit": None,
        "game_limit": None,
        "window_days": None,
        "storage_bytes": None,
        "category_limit": None,
        # C-4 default: architecture ships, sales don't — checkout for this
        # key is rejected server-side until the owner enables it.
        "enabled": False,
        "blurb": "Everything in Venue plus recurring tournaments. Coming soon.",
    },
    "party_game_reactivation": {
        "display_name": "Party Game reactivation",
        "kind": EntitlementKind.PARTY_PACK,
        "mode": "payment",
        "price_env": "STRIPE_PRICE_PARTY_GAME_REACTIVATION",
        "display_price": "$4.99",
        "interval": None,
        "reactivation_of": EntitlementKind.PARTY_PACK,
        "window_days": 30,  # extension length
        "enabled": True,  # webhook path live; no storefront button (dark)
        "blurb": "Reopen an expired Party Game for another 30 days.",
    },
    "big_game_reactivation": {
        "display_name": "Big Game reactivation",
        "kind": EntitlementKind.BIG_PACK,
        "mode": "payment",
        "price_env": "STRIPE_PRICE_BIG_GAME_REACTIVATION",
        "display_price": "$9.99",
        "interval": None,
        "reactivation_of": EntitlementKind.BIG_PACK,
        "window_days": 30,
        "enabled": True,
        "blurb": "Reopen an expired Big Game for another 30 days.",
    },
}

# §F2 union: account-scoped allowances a kind contributes while ACTIVE
# (merged max-wise over PLAN_LIMITS in accounts/quotas.limits_for). Defaults
# FLAGGED in CHANGES: venue reuses the creator plan's category/storage
# numbers; its question total is uncapped because the 100-ACTIVE gate at the
# question choke point is the real venue limit (see billing/access.py).
ACCOUNT_GRANTS = {
    EntitlementKind.VENUE: {
        "categories": 25,
        "questions": None,  # unlimited TOTAL; 100-ACTIVE enforced separately
        "storage_bytes": 500 * MB,
    },
    EntitlementKind.VENUE_TOURNAMENT: {
        "categories": 25,
        "questions": None,
        "storage_bytes": 500 * MB,
        "tournaments": 25,  # unreachable until C-4 enables sales
    },
}

VENUE_ACTIVE_QUESTION_LIMIT = 100  # §F6: active (non-archived) questions


def billing_enabled() -> bool:
    """The Resend/Turnstile pattern: billing is ON iff the secret key is
    set. Without it, /checkout/ and /portal/ answer a clean 503-style
    'billing not configured'; /products/ and /status/ still serve (they
    need no Stripe)."""
    return bool(settings.STRIPE_SECRET_KEY)


def get_product(key: str) -> dict | None:
    return PRODUCTS.get(key)


def price_id_for(key: str) -> str:
    entry = PRODUCTS.get(key) or {}
    return getattr(settings, entry.get("price_env", ""), "") or ""


def configure_stripe():
    """Set the SDK key + PIN the API version explicitly (§F2).

    Version note (verified at build time against stripe 15.4.0, API
    2026-07-29.dahlia): `current_period_start/end` live on the
    SUBSCRIPTION ITEM, not the Subscription — billing/services.py reads
    them from items.data[0], with the invoice's subscription id read from
    the top-level field falling back to parent.subscription_details.
    Bump BOTH the pin in requirements.txt and this comment together.
    """
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2026-07-29.dahlia"
    return stripe


def public_products() -> list[dict]:
    """Display fields only (the /products/ payload) — never Price IDs."""
    rows = []
    for key, entry in PRODUCTS.items():
        if entry.get("reactivation_of"):
            continue  # dark lane: no storefront listing v1
        rows.append(
            {
                "key": key,
                "name": entry["display_name"],
                "price": entry["display_price"],
                "interval": entry["interval"],
                "blurb": entry["blurb"],
                "coming_soon": not entry["enabled"],
            }
        )
    return rows
