from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("", views.cart_view, name="cart"),
    path("add/<int:product_id>/", views.cart_add, name="cart_add"),
    path("update/<int:product_id>/", views.cart_update, name="cart_update"),
    path("remove/<int:product_id>/", views.cart_remove, name="cart_remove"),
    path("checkout/", views.checkout_view, name="checkout"),
    path(
        "checkout/confirmation/<str:payment_reference>/",
        views.order_confirmation_view,
        name="order_confirmation",
    ),
]
