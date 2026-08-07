"""§F3 (Handoff #18): billing transactional email.

Voice rule 8: billing/receipt copy is deliberately PLAIN — no bar-night
quips. Same never-break-the-action contract as accounts/emails.py.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _send(subject: str, body: str, to_email: str) -> None:
    try:
        send_mail(subject, body, None, [to_email])
    except Exception:  # noqa: BLE001 — see accounts/emails.py
        logger.warning("Billing email send failed (subject=%r, to=%r)", subject, to_email, exc_info=True)


def _money(amount_minor: int | None, currency: str) -> str:
    """Integer minor units → display ("$9.99 USD"). Symbol only for USD —
    C-8 says USD-only v1; anything else prints the code alone."""
    if amount_minor is None:
        return ""
    code = (currency or "usd").upper()
    value = f"{amount_minor / 100:.2f}"
    return f"${value} {code}" if code == "USD" else f"{value} {code}"


def send_purchase_confirmation(
    user,
    product_name: str,
    category_name: str | None,
    *,
    guide_to_create: bool = False,
    amount_total: int | None = None,
    currency: str = "",
    purchased_at=None,
    reference: str = "",
) -> None:
    """The buyer's confirmation AND receipt (owner ruling: the app is the
    receipt sender; Stripe's customer emails stay optional/off). The money
    block is the record: product, amount, date, payment reference."""
    lines = [
        f"Thanks — your {product_name} purchase is confirmed.",
        "",
    ]
    if category_name:
        lines += [
            f'A starter category, "{category_name}", is ready in your library. '
            "Add your questions there; your pack's budget and days remaining "
            "show on your profile's billing panel.",
            "",
        ]
    elif guide_to_create:
        # #19.1: no auto-created starter — point FRESH pack buyers at
        # /create, where the category form now offers the pack lane.
        # Reactivations keep their existing content and skip this line.
        lines += [
            'Head to the "Your content" page to set up categories for your '
            "purchase and add questions; your budget and days remaining "
            "show on your profile's billing panel.",
            "",
        ]
    receipt = [f"Receipt — {product_name}"]
    if amount_total is not None:
        receipt.append(f"Amount: {_money(amount_total, currency)}")
    if purchased_at is not None:
        receipt.append(f"Date: {purchased_at:%B %d, %Y}")
    if reference:
        receipt.append(f"Payment reference: {reference}")
    lines += receipt + [
        "",
        "Keep this email for your records.",
        "",
        "— Drinkquiry",
    ]
    _send(f"Your Drinkquiry {product_name} is ready", "\n".join(lines), user.email)


def send_renewal_receipt(
    user, product_name: str, amount_paid: int, currency: str, period_end
) -> None:
    """One receipt per successful subscription charge (invoice.paid) — the
    self-receipting rule needs renewals covered too, not just day one."""
    through = f" Your subscription is paid through {period_end:%B %d, %Y}." if period_end else ""
    _send(
        f"Receipt — Drinkquiry {product_name}",
        (
            f"Payment received: {_money(amount_paid, currency)} for your "
            f"{product_name} subscription.{through}\n\n"
            "Manage your subscription (payment method, invoices, cancellation) "
            "any time from the Manage billing button on your profile.\n\n"
            "Keep this email for your records.\n\n"
            "— Drinkquiry"
        ),
        user.email,
    )


def send_subscription_started(user, product_name: str) -> None:
    _send(
        f"Your Drinkquiry {product_name} subscription is active",
        (
            f"Thanks — your {product_name} subscription is active.\n\n"
            "Manage it any time (payment method, invoices, cancellation) from "
            "the Manage billing button on your profile.\n\n"
            "— Drinkquiry"
        ),
        user.email,
    )


def send_payment_failed(user, product_name: str, portal_hint: bool = True) -> None:
    body = (
        f"A payment for your {product_name} subscription didn't go through.\n\n"
        f"Your access continues for a {settings.BILLING_GRACE_DAYS}-day grace "
        "period while the payment retries. To update your card, use the "
        "Manage billing button on your profile.\n\n"
        "Nothing you've made is ever deleted.\n\n"
        "— Drinkquiry"
    )
    _send(f"Payment issue with your Drinkquiry {product_name}", body, user.email)
