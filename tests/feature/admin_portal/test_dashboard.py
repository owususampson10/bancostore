from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

User = get_user_model()


def _dashboard_url():
    return reverse("admin_portal:dashboard")


@pytest.mark.django_db
def test_staff_can_reach_the_dashboard(staff_client):
    response = staff_client.get(_dashboard_url())

    assert response.status_code == 200
    assert b"You're logged in" in response.content


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    user = User.objects.create_user(
        username="+233249999999", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_dashboard_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_dashboard_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url
