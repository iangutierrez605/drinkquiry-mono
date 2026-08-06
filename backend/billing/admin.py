from django.contrib import admin

from .models import (
    BillingAccount,
    BillingAuditLog,
    Entitlement,
    Purchase,
    StripeEvent,
    Subscription,
)

# Minimal read-heavy registrations — the real staff surface is §F9's
# /manage/billing; Django admin is the escape hatch meanwhile.


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("stripe_event_id", "event_type", "status", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id",)


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("user", "product_key", "status", "amount_total", "currency", "purchased_at")
    list_filter = ("status", "product_key")
    search_fields = ("user__email", "stripe_checkout_session_id", "stripe_payment_intent_id")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "product_key", "status", "current_period_end", "cancel_at_period_end")
    list_filter = ("status", "product_key")
    search_fields = ("user__email", "stripe_subscription_id")


@admin.register(Entitlement)
class EntitlementAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "active_from", "active_until")
    list_filter = ("kind",)
    search_fields = ("user__email",)


admin.site.register(BillingAccount)
admin.site.register(BillingAuditLog)
