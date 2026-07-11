import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

import pytest

from apps.accounts.models import CustomerProfile

User = get_user_model()

RESET_LINK_PATTERN = re.compile(r"http\S+password/reset/key/\S+")


@pytest.mark.django_db
def test_customer_can_register_logout_and_login(client):
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Ama Mensah",
            "email": "ama@example.test",
            "phone_number": "+233241234567",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )

    # Signup redirects to LOGIN_REDIRECT_URL, which doesn't have a real page yet
    # (no homepage until Task 8) — a 302 here confirms signup itself succeeded.
    assert response.status_code == 302
    user = User.objects.get(email="ama@example.test")
    assert user.customer_profile.full_name == "Ama Mensah"
    assert str(user.customer_profile.phone_number) == "+233241234567"
    assert user.groups.filter(name="customer").exists()

    client.post(reverse("account_logout"))
    assert "_auth_user_id" not in client.session

    login_response = client.post(
        reverse("account_login"),
        {"login": "ama@example.test", "password": "S3cure-Passw0rd!"},
    )

    assert login_response.status_code == 302
    assert int(client.session["_auth_user_id"]) == user.id


@pytest.mark.django_db
def test_customer_can_register_with_a_local_format_phone_number(client):
    """Ghana numbers typed without +233 (e.g. "0545488681", as a real user
    would type it) must still validate — PHONENUMBER_DEFAULT_REGION makes
    this work. Regression test for a bug caught during manual testing."""
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Kojo Mensah",
            "email": "kojo2@example.test",
            "phone_number": "0545488681",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )

    assert response.status_code == 302
    user = User.objects.get(email="kojo2@example.test")
    assert str(user.customer_profile.phone_number) == "+233545488681"


@pytest.mark.django_db
def test_customer_cannot_register_without_accepting_terms(client):
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Kojo Antwi",
            "email": "kojo@example.test",
            "phone_number": "+233241234568",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(email="kojo@example.test").exists()
    assert "terms_accepted" in response.context["form"].errors


@pytest.mark.django_db
def test_customer_password_reset_completes_via_email_link(client):
    user = User.objects.create_user(
        username="kwame", email="kwame@example.test", password="OldPassw0rd!"
    )
    CustomerProfile.objects.create(
        user=user, full_name="Kwame Boateng", phone_number="+233551234567"
    )

    client.post(reverse("account_reset_password"), {"email": "kwame@example.test"})

    assert len(mail.outbox) == 1
    match = RESET_LINK_PATTERN.search(mail.outbox[0].body)
    assert match, "password reset email did not contain a reset link"
    reset_url = match.group(0)

    set_password_response = client.get(reset_url, follow=True)
    assert set_password_response.status_code == 200

    final_url = set_password_response.redirect_chain[-1][0]
    client.post(
        final_url,
        {"password1": "NewPassw0rd!", "password2": "NewPassw0rd!"},
    )

    user.refresh_from_db()
    assert user.check_password("NewPassw0rd!")
