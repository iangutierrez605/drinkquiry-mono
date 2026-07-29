from rest_framework import permissions


class IsOwnerOrReadOnlyPublic(permissions.BasePermission):
    """Owners can edit their content; everyone can read what the queryset exposes."""

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.owner_id == request.user.id


class IsCreator(permissions.BasePermission):
    """Custom content is a paid feature, keyed off the user's plan.

    `user.is_creator` is now derived: effective plan (expiry-aware) != "free".
    Creation (POST) is allowed through here for any authenticated user because
    the view's quota check governs it — free plans carry a limit of 0, so free
    users get the structured `quota_*` 403 (with used/limit) instead of a bare
    permission message, and a future free allowance is a settings edit away.
    Updates/deletes still require an active paid plan, as before.
    """

    message = "Creating custom categories and questions requires a creator account."

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if not (request.user and request.user.is_authenticated):
            return False
        if getattr(view, "action", None) in ("create", "bulk"):
            # quota checks in the view decide (structured 403 for free users);
            # `bulk` also covers staff official uploads, which are quota-exempt
            # and must not require a paid plan.
            return True
        return request.user.is_creator
