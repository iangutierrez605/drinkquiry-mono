from django.contrib.auth import login
from django.contrib.auth import password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.conf import settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from knox.models import AuthToken
from knox.views import LoginView as KnoxLoginView
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .emails import send_password_changed_email, send_password_reset_email
from .models import User
from .serializers import AuthSerializer, UserSerializer
from .throttling import (
    LoginRateThrottle,
    PasswordForgotRateThrottle,
    PasswordResetRateThrottle,
    RegisterRateThrottle,
)
from .turnstile import turnstile_enabled, verify_turnstile


class RegisterView(generics.CreateAPIView):
    serializer_class = UserSerializer
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (RegisterRateThrottle,)  # §I2: 5/min per IP

    def create(self, request, *args, **kwargs):
        """§F (Handoff #12): layered bot gates, BEFORE the serializer runs.
        A view override, not middleware — the gates belong to the one view
        they protect. Both 400 bodies are NEW documented shapes (C4: added,
        nothing mutated); both are deliberately vague (never name the field,
        never hint the mechanism).
        """
        # F1 — honeypot, always on: the register form renders a decoy
        # `website` input that humans never see. A NON-EMPTY value is a bot;
        # an empty or absent key proceeds normally, so every existing client
        # (and the smoke, C12) is untouched.
        if str(request.data.get("website") or "").strip():
            return Response({"detail": "Registration failed."}, status=status.HTTP_400_BAD_REQUEST)
        # F2 — Cloudflare Turnstile, opt-in via env (ON iff the secret is
        # set; entirely absent otherwise — a stray turnstile_token is
        # ignored when OFF). Verification fails CLOSED (see turnstile.py).
        if turnstile_enabled():
            token = str(request.data.get("turnstile_token") or "")
            if not verify_turnstile(token, remoteip=request.META.get("REMOTE_ADDR")):
                return Response(
                    {"detail": "Verification failed — please try again."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return super().create(request, *args, **kwargs)


class LoginView(KnoxLoginView):
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (LoginRateThrottle,)  # §I2: 10/min per IP

    def post(self, request, format=None):
        serializer = AuthSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        login(request, serializer.validated_data["user"])
        return super().post(request, format=None)

    def get_user_serializer_class(self):
        # knox includes serialized user in the login response
        return UserSerializer


BRAND_WRITE_FIELDS = {"brand_name", "brand_logo", "brand_logo_clear"}


class ProfileView(generics.RetrieveUpdateAPIView):
    """§H (Handoff #11): the profile PATCH now also carries venue branding
    (brand_name, brand_logo multipart, brand_logo_clear). Branding isn't a
    counted quota, so a WRITE without a branding lane (manual creator plan
    OR an active venue-kind entitlement — §F3(d), Handoff #19) gets a plain
    403 (reads are fine and the fields persist through any lapse — only
    SERVING is lane-gated, over in the games snapshot). The storage quota
    DOES apply to the upload (the standard structured quota_storage 403,
    same as category photos)."""

    serializer_class = UserSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        if BRAND_WRITE_FIELDS & set(request.data.keys()):
            # §F3(d) (Handoff #19): plan alone is wrong for buyers (§A.1) —
            # Stripe writes entitlements, never `plan`, and the Venue promise
            # is "your branding on every screen". Widened to: manual creator
            # OR an ACTIVE venue-kind entitlement (packs don't include
            # branding). Lazy import — the accounts↔billing convention.
            from billing.access import venue_active

            if not (request.user.is_creator or venue_active(request.user)):
                return Response(
                    {"detail": "Branding is part of the Venue plan."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            logo = request.FILES.get("brand_logo")
            if logo is not None:
                from .quotas import storage_quota_denial

                if denial := storage_quota_denial(request.user, logo.size):
                    return Response(denial, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)


# --- §K1/§K2 (Handoff #9): password flows -----------------------------------

FORGOT_RESPONSE = {"detail": "If that account exists, a reset link is on its way."}
FORGOT_COOLDOWN_SECONDS = 60


class PasswordForgotView(APIView):
    """POST /api/auth/password/forgot/ {email}.

    ALWAYS the same 200 body, account or not (no user enumeration — pinned).
    When the account exists: email a link to
    {FRONTEND_BASE_URL}/reset-password?uid=<b64 pk>&token=<Django reset token>.
    Rate limiting is a per-email 60s cache cooldown; hitting it silently
    skips the SEND and never changes the response body (enumeration again).
    §I2 (Handoff #10) layered a per-IP throttle ON TOP (no longer §M): the
    cooldown and the throttle are independent — a 429 is a different STATUS,
    which is fine (C4); every 200 keeps the identical pinned body.
    """

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (PasswordForgotRateThrottle,)  # §I2: 5/min per IP

    def post(self, request):
        email = str(request.data.get("email") or "").strip()
        if not email:
            return Response({"email": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            cooldown_key = f"pwreset-cooldown:{user.pk}"
            if cache.add(cooldown_key, 1, FORGOT_COOLDOWN_SECONDS):  # atomic set-if-absent
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                reset_url = f"{settings.FRONTEND_BASE_URL}/reset-password?uid={uid}&token={token}"
                send_password_reset_email(user, reset_url)
        return Response(FORGOT_RESPONSE)


def _validate_new_password(new_password, user):
    """Run Django's validators; return an error Response or None."""
    if not new_password:
        return Response({"new_password": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)
    try:
        password_validation.validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        return Response({"new_password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
    return None


class PasswordResetView(APIView):
    """POST /api/auth/password/reset/ {uid, token, new_password}.

    Invalid/expired uid+token → a generic 400 (never "no such user").
    Success sets the password and REVOKES EVERY Knox token for the user —
    a reset after "someone might know my password" must kill live sessions.
    """

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (PasswordResetRateThrottle,)  # §I2: 5/min per IP

    def post(self, request):
        uid = str(request.data.get("uid") or "")
        token = str(request.data.get("token") or "")
        new_password = str(request.data.get("new_password") or "")
        user = None
        try:
            user = User.objects.filter(pk=int(urlsafe_base64_decode(uid).decode()), is_active=True).first()
        except (ValueError, TypeError, OverflowError):
            user = None
        if user is None or not default_token_generator.check_token(user, token):
            return Response(
                {"detail": "That reset link is invalid or has expired — request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if error := _validate_new_password(new_password, user):
            return error
        user.set_password(new_password)
        user.save(update_fields=["password"])
        AuthToken.objects.filter(user=user).delete()  # kill ALL live sessions
        return Response({"detail": "Password reset. Log in with your new password."})


class PasswordChangeView(APIView):
    """POST /api/auth/password/change/ {current_password, new_password}.

    Knox-authed. Wrong current → 400. Success keeps THIS session's token and
    revokes every other one, then emails a heads-up notification (fail-silent
    — the email must never break the change)."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        user = request.user
        current = str(request.data.get("current_password") or "")
        new_password = str(request.data.get("new_password") or "")
        if not user.check_password(current):
            return Response(
                {"current_password": ["That doesn't match your current password."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if error := _validate_new_password(new_password, user):
            return error
        user.set_password(new_password)
        user.save(update_fields=["password"])
        # request.auth is this request's knox AuthToken instance — keep it,
        # kill the rest.
        qs = AuthToken.objects.filter(user=user)
        if request.auth is not None:
            qs = qs.exclude(pk=request.auth.pk)
        qs.delete()
        send_password_changed_email(user)
        return Response({"detail": "Password changed. Other sessions were signed out."})
