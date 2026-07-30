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


class RegisterView(generics.CreateAPIView):
    serializer_class = UserSerializer
    permission_classes = (permissions.AllowAny,)


class LoginView(KnoxLoginView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request, format=None):
        serializer = AuthSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        login(request, serializer.validated_data["user"])
        return super().post(request, format=None)

    def get_user_serializer_class(self):
        # knox includes serialized user in the login response
        return UserSerializer


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_object(self):
        return self.request.user


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
    Proper per-IP rate limiting is punted (§M).
    """

    permission_classes = (permissions.AllowAny,)

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
