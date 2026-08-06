from django.urls import path

from .views import CheckoutView, PortalView, ProductsView, StatusView, WebhookView

urlpatterns = [
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("webhook/", WebhookView.as_view(), name="billing-webhook"),
    path("status/", StatusView.as_view(), name="billing-status"),
    path("products/", ProductsView.as_view(), name="billing-products"),
    path("portal/", PortalView.as_view(), name="billing-portal"),
]
