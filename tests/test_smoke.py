from django.urls import reverse

import pytest


@pytest.mark.django_db
def test_admin_login_redirects_to_two_factor_login(client):
    response = client.get(reverse("admin:login"), follow=True)

    assert response.status_code == 200
    assert response.redirect_chain[-1][0].startswith(reverse("two_factor:login"))


@pytest.mark.django_db
def test_two_factor_login_page_loads(client):
    response = client.get(reverse("two_factor:login"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_allauth_signup_page_loads(client):
    response = client.get(reverse("account_signup"))

    assert response.status_code == 200
