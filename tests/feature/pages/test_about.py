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


@pytest.mark.django_db
def test_about_page_covers_all_three_real_user_roles(client):
    """CodeRabbit finding on PR #56: the page only described Customers
    and Distributors -- SPEC.md defines a third real role, Admin (runs
    the business: KYC review, withdrawal approval, product/order
    management, business-rule settings), and the page's own acceptance
    criteria call for covering all three, not two plus an unrelated
    "Verified & secure" marketing card."""
    response = client.get(reverse("pages:about"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Customers" in content
    assert "Distributors" in content
    assert "Admin" in content

    content_lower = content.lower()
    for marker in ("kyc", "withdrawal", "product", "order", "settings panel"):
        assert marker in content_lower
