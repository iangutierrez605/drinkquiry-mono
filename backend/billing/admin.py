from django.contrib import admin
from django.utils import timezone

from .models import (
    BillingAccount,
    BillingAuditLog,
    Entitlement,
    Purchase,
    StripeEvent,
    StripeEventStatus,
    Subscription,
)

# Minimal read-heavy registrations — the real staff surface is §F9's
# /manage/billing; Django admin is the escape hatch meanwhile.


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("stripe_event_id", "event_type", "status", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id",)
    actions = ("retry_failed",)

    @admin.action(description="Retry processing — failed rows only")
    def retry_failed(self, request, queryset):
        """§F6 (Handoff #19): re-run the processor on the STORED plain-JSON
        payload — it exists precisely for this (no Stripe round trip).
        Bookkeeping mirrors the webhook view exactly (processed/skipped on
        success + processed_at; failed + error on another failure); every
        handler is idempotent, so retrying a half-succeeded event grants
        nothing twice. Non-failed rows are silently skipped (C-6 guard)."""
        from .services import ProcessingError, process_event

        retried = succeeded = 0
        for event in queryset.filter(status=StripeEventStatus.FAILED):
            retried += 1
            try:
                outcome = process_event(event.payload)
            except ProcessingError as exc:
                event.status = StripeEventStatus.FAILED
                event.error = str(exc)
                event.save(update_fields=["status", "error"])
                continue
            except Exception as exc:  # noqa: BLE001 — recorded, same as the view
                event.status = StripeEventStatus.FAILED
                event.error = repr(exc)
                event.save(update_fields=["status", "error"])
                continue
            event.status = (
                StripeEventStatus.SKIPPED if outcome == "skipped" else StripeEventStatus.PROCESSED
            )
            event.error = ""  # a stale error under a green status would mislead
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "error", "processed_at"])
            succeeded += 1
        self.message_user(
            request,
            f"Retried {retried} failed event(s): {succeeded} now processed/skipped, "
            f"{retried - succeeded} still failed.",
        )


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
