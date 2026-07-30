from django.urls import path
from knox import views as knox_views

from .views import (
    LoginView,
    PasswordChangeView,
    PasswordForgotView,
    PasswordResetView,
    ProfileView,
    RegisterView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="knox_login"),
    path("logout/", knox_views.LogoutView.as_view(), name="knox_logout"),
    path("logoutall/", knox_views.LogoutAllView.as_view(), name="knox_logoutall"),
    path("profile/", ProfileView.as_view(), name="profile"),
    # §K (Handoff #9): password flows. forgot/reset are AllowAny (that's the
    # point); change is Knox-authed.
    path("password/forgot/", PasswordForgotView.as_view(), name="password-forgot"),
    path("password/reset/", PasswordResetView.as_view(), name="password-reset"),
    path("password/change/", PasswordChangeView.as_view(), name="password-change"),
]
