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
        # §F4 (#18): pack buyers and venue subscribers are NOT plan
        # "creator" (never rendered as such — brief rule) but must edit,
        # archive and delete their own content. The permission layer lets
        # ANY entitlement-owner through — active OR lapsed — so the views
        # can answer object-aware: bound content under a lapsed pack gets
        # the informative pack_inactive 403 (naming reactivation) instead
        # of a generic denial, while UNBOUND writes without an ACTIVE lane
        # re-deny in the view (can_paid_write — the lapsed-creator
        # read-only precedent, kept).
        from billing.access import can_paid_write, owns_any_entitlement

        return can_paid_write(request.user) or owns_any_entitlement(request.user)
