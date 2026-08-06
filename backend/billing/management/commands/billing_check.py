"""`manage.py billing_check` (Handoff #18 follow-up): diagnose the Stripe
configuration from the server's own env, read-only, in seconds.

Answers the three questions behind every "Couldn't start checkout" 502:
1. Is the SECRET KEY accepted, and can this box reach api.stripe.com?
2. Does every configured PRICE ID exist in the SAME mode as the key
   (test key + live price id — the classic — reads "No such price")?
3. Do the price's mode/amount agree with the catalog entry?

Read-only: one Customer.list(limit=1) probe + Price.retrieve per id.
Never prints the key itself, only its mode prefix.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from billing.catalog import PRODUCTS, billing_enabled, configure_stripe, price_id_for


class Command(BaseCommand):
    help = "Diagnose Stripe billing config: key validity, reachability, every price id."

    def handle(self, *args, **options):
        out = self.stdout.write
        if not billing_enabled():
            out("STRIPE_SECRET_KEY is unset — billing is OFF (checkout answers "
                "billing_not_configured). Nothing else to check.")
            return
        key = settings.STRIPE_SECRET_KEY
        mode = "TEST" if key.startswith("sk_test_") else "LIVE" if key.startswith("sk_live_") else "UNRECOGNIZED PREFIX"
        out(f"Secret key: set ({mode} mode). Price ids below must come from the SAME mode "
            "and the same Stripe account.")
        if key != key.strip():
            out("⚠️  The key has leading/trailing whitespace (a .env copy-paste classic) — fix that first.")

        stripe = configure_stripe()
        try:
            stripe.Customer.list(limit=1)
            out("✓ key accepted; api.stripe.com reachable from this box.")
        except Exception as exc:  # noqa: BLE001 — this command's whole job
            name = type(exc).__name__
            if "Authentication" in name:
                out(f"✗ key REJECTED by Stripe ({name}). Wrong key, revoked key, or a "
                    "restricted key without the needed permissions.")
            elif "Connection" in name:
                out(f"✗ can't reach api.stripe.com ({name}) — outbound network/proxy/"
                    "firewall on this server.")
            else:
                out(f"✗ probe failed: {name}: {exc}")
            return

        problems = 0
        for product_key, entry in PRODUCTS.items():
            price_id = price_id_for(product_key)
            label = f"{product_key} ({entry['price_env']})"
            if not price_id:
                note = "checkout for it answers billing_not_configured"
                if not entry["enabled"]:
                    note = "fine while it's coming-soon"
                out(f"– {label}: env unset — {note}.")
                continue
            try:
                price = stripe.Price.retrieve(price_id)
            except Exception as exc:  # noqa: BLE001
                out(f"✗ {label}: {price_id} → {type(exc).__name__}: {exc}")
                out("   (\"No such price\" almost always means the id is from the other "
                    "mode — test vs live — or a different Stripe account.)")
                problems += 1
                continue
            is_recurring = bool(price.get("recurring"))
            wants_recurring = entry["mode"] == "subscription"
            amount = price.get("unit_amount")
            currency = str(price.get("currency") or "").upper()
            shown = f"{amount / 100:.2f} {currency}" if amount is not None else "no unit_amount"
            line = f"✓ {label}: {price_id} · {shown} · {'recurring' if is_recurring else 'one-time'}"
            if not price.get("active"):
                line += " · ⚠️ INACTIVE in Stripe"
                problems += 1
            if is_recurring != wants_recurring:
                line += f" · ⚠️ MODE MISMATCH (catalog says {entry['mode']})"
                problems += 1
            if entry.get("display_price") and amount is not None:
                display = entry["display_price"].lstrip("$")
                if f"{amount / 100:.2f}" != display:
                    line += f" · note: display price {entry['display_price']} ≠ Stripe amount"
            out(line)

        base = settings.FRONTEND_BASE_URL
        if not base.startswith(("http://", "https://")):
            out(f"⚠️ FRONTEND_BASE_URL ({base!r}) has no scheme — Stripe rejects the "
                "success/cancel URLs built from it.")
            problems += 1
        if not settings.STRIPE_WEBHOOK_SECRET:
            out("– STRIPE_WEBHOOK_SECRET unset: checkout will work but NOTHING FULFILLS "
                "(the webhook is the only granter). Set it before testing a purchase.")
        out("All good — a checkout should go through." if problems == 0
            else f"{problems} problem(s) above — fix those and retry.")
