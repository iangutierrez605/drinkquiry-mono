from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "display_name", "plan", "plan_expires_at", "creator_access", "is_staff", "date_joined")
    list_filter = ("plan", "is_staff", "is_active")
    search_fields = ("email", "display_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("display_name", "date_of_birth")}),
        # Manual grant flow (pre-Stripe): set plan="creator" here, optionally
        # with an expiry. This replaces the old "tick is_creator" workflow.
        ("Entitlements", {"fields": ("plan", "plan_expires_at")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )

    @admin.display(boolean=True, description="creator access")
    def creator_access(self, obj):
        return obj.is_creator
