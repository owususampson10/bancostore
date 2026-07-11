from django.urls import reverse

import pytest

from apps.catalog.models import Category


@pytest.mark.django_db
def test_staff_can_create_a_category_via_django_admin(staff_client):
    response = staff_client.post(
        reverse("admin:catalog_category_add"),
        {"name": "Watches", "slug": "watches"},
    )

    assert response.status_code == 302
    category = Category.objects.get(name="Watches")
    assert category.slug == "watches"
