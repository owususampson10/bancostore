from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.address_list, name="address_list"),
    path("add/", views.address_create, name="address_create"),
    path("<int:pk>/edit/", views.address_edit, name="address_edit"),
    path("<int:pk>/delete/", views.address_delete, name="address_delete"),
]
