from django.urls import reverse

import pytest


@pytest.mark.django_db
def test_about_page_renders(client):
    response = client.get(reverse("pages:about"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_about_page_describes_the_real_platform(client):
    """Content grounded in SPEC.md's own Objective section -- what
    Bancostore actually is (Ghana ecommerce + binary-MLM platform) and
    the three real user roles -- not invented company history."""
    response = client.get(reverse("pages:about"))

    content = response.content.decode()
    assert "Ghana" in content
    assert "Distributor" in content or "distributor" in content
