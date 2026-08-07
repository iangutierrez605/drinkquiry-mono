"""Transactional email helpers (Handoff #9 §K).

One rule above all: SENDING MUST NEVER BREAK THE ACTION. Every helper here
catches Exception, logs a warning, and returns — an email outage must not
turn a password change or a moderation approve into a 500 (pinned by a test
that mocks a raising backend).

Plain text only (HTML templates are §M). The backend is env-driven in
settings.py: with RESEND_API_KEY set it's Anymail's Resend backend; without
it (dev, tests, the suite) it's the console/locmem backend, so nothing here
ever needs a key to run.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _send(subject: str, body: str, to_email: str) -> None:
    """The one send body: default from-address, warn-and-continue on failure."""
    try:
        send_mail(subject, body, None, [to_email])  # None → DEFAULT_FROM_EMAIL
    except Exception:  # noqa: BLE001 — deliberately broad; see module docstring
        logger.warning("Email send failed (subject=%r, to=%r)", subject, to_email, exc_info=True)


def greeting_name(user) -> str:
    """§G (Handoff #13): what an email calls the person — display name if
    set, else the email's LOCAL-PART ("sam", not "sam@gmail.com"). The ONE
    place this rule lives (C1); every greeting below flows through it — the
    same full-email fallback #12 killed in the navbar, killed here too.
    Never returns a full address."""
    if user.display_name:
        return user.display_name
    return (user.email or "").split("@")[0]


def send_password_reset_email(user, reset_url: str) -> None:
    """§K1: the forgot-password link. The caller guarantees the user exists —
    the enumeration-safe 200 is the VIEW's job, not this helper's."""
    _send(
        "Reset your Drinkquiry password",
        (
            f"Hi {greeting_name(user)},\n\n"
            "Someone (hopefully you) asked to reset the password for this "
            "Drinkquiry account. Follow this link to pick a new one:\n\n"
            f"{reset_url}\n\n"
            "The link expires after a while. If you didn't ask for this, you "
            "can ignore this email — your password is unchanged.\n\n"
            "— Drinkquiry"
        ),
        user.email,
    )


def send_password_changed_email(user) -> None:
    """§K2: notification after a signed-in password change."""
    _send(
        "Your Drinkquiry password was changed",
        (
            f"Hi {greeting_name(user)},\n\n"
            "Your Drinkquiry password was just changed, and every other "
            "signed-in session was logged out.\n\n"
            "If this wasn't you, reset your password right away from the "
            "login screen ('Forgot password?').\n\n"
            "— Drinkquiry"
        ),
        user.email,
    )


def send_moderation_outcome_email(item, *, approved: bool, actor) -> None:
    """§K3: approval/rejection notification to a creator.

    `item` is a Question or a Category (duck-typed on the fields both share).
    Skip rules (both deliberate):
      - owner is None → official content, nobody to notify;
      - actor IS the owner → staff self-approval noise.
    Reject includes the moderation note — the owner asked for approved-only,
    but reject-with-reason is the same hook and kinder than silence
    (delegated default; remove the `else` branch to send approvals only).
    """
    owner = item.owner
    # actor == owner: #19: deliberate — the solo-staff owner approving
    # their own test content lands here; see HANDOFF #19 §F1. (C-1: staff
    # authoring under their own account shouldn't self-spam. If
    # self-approval mail is ever wanted, delete the actor clause — one
    # line — and drop trivia's test_self_approval_sends_nothing pin.)
    if owner is None or (actor is not None and getattr(actor, "pk", None) == owner.pk):
        return
    kind = "question" if hasattr(item, "question_text") else "category"
    label = item.question_text if kind == "question" else item.name
    if len(label) > 80:
        label = label[:77] + "…"
    # §F (Handoff #10): questions live in one or MORE categories now — a
    # C6-grep find this handoff's known list missed (noted in CHANGES.md).
    if kind == "question":
        names = sorted(c.name for c in item.categories.all())
        where = f" in {', '.join(names)}" if names else ""
    else:
        where = ""
    if approved:
        subject = f"Your {kind} was approved"
        body = (
            f"Hi {greeting_name(owner)},\n\n"
            f"Good news — your {kind} '{label}'{where} passed review and is "
            "now live for everyone's games.\n\n"
            "— Drinkquiry"
        )
    else:
        subject = f"Your {kind} wasn't approved"
        body = (
            f"Hi {greeting_name(owner)},\n\n"
            f"A moderator reviewed your {kind} '{label}'{where} and didn't "
            "approve it for public play. Their note:\n\n"
            f"  {item.moderation_note or '(no note)'}\n\n"
            "You can edit it and resubmit from your content page — it stays "
            "available in your own private games either way.\n\n"
            "— Drinkquiry"
        )
    _send(subject, body, owner.email)
