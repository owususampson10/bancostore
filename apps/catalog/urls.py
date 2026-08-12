from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("shop/", views.product_list, name="product_list"),
    path("shop/<slug:slug>/", views.product_detail, name="product_detail"),
    path("wishlist/", views.wishlist_view, name="wishlist"),
    path(
        "wishlist/toggle/<int:product_id>/",
        views.wishlist_toggle,
        name="wishlist_toggle",
    ),
    path(
        "reviews/submit/<int:product_id>/",
        views.review_submit,
        name="review_submit",
    ),
]
