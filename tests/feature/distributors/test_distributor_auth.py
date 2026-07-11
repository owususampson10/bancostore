from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.notifications.sms import fake_outbox

User = get_user_model()


@pytest.mark.django_db
def test_distributor_can_register_verify_otp_and_login(client):
    response = client.post(
        reverse("distributors:register"),
        {
            "phone_number": "+233241234567",
            "email": "kwabena@example.test",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )
    assert response.status_code == 302

    distributor = Distributor.objects.get(phone_number="+233241234567")
    assert distributor.phone_verified is False
    assert distributor.user.groups.filter(name="distributor").exists()

    # A real OTP was "sent" via the fake sender — pull the actual code out of it
    # rather than reaching into the database, since that's what a real user does.
    assert fake_outbox, "no OTP SMS was sent on registration"
    sent_message = fake_outbox[-1]["message"]
    code = "".join(ch for ch in sent_message if ch.isdigit())[:6]

    verify_response = client.post(reverse("distributors:verify_otp"), {"code": code})
    assert verify_response.status_code == 302

    distributor.refresh_from_db()
    assert distributor.phone_verified is True
    assert int(client.session["_auth_user_id"]) == distributor.user.id

    client.post(reverse("distributors:logout"))
    assert "_auth_user_id" not in client.session

    login_response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233241234567", "password": "S3cure-Passw0rd!"},
    )
    assert login_response.status_code == 302
    assert int(client.session["_auth_user_id"]) == distributor.user.id


@pytest.mark.django_db
def test_wrong_otp_does_not_verify_the_phone(client):
    client.post(
        reverse("distributors:register"),
        {
            "phone_number": "+233241234567",
            "email": "kwabena@example.test",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )

    response = client.post(reverse("distributors:verify_otp"), {"code": "000000"})

    assert response.status_code == 200
    distributor = Distributor.objects.get(phone_number="+233241234567")
    assert distributor.phone_verified is False


def _create_verified_distributor(phone_number="+233551234567", password="Passw0rd!"):
    user = User.objects.create_user(username=phone_number, password=password)
    return Distributor.objects.create(
        user=user, phone_number=phone_number, phone_verified=True
    )


@pytest.mark.django_db
def test_n_failed_logins_locks_the_account_for_the_configured_duration(client):
    distributor = _create_verified_distributor()

    for _ in range(config.MAX_FAILED_LOGIN_ATTEMPTS):
        response = client.post(
            reverse("distributors:login"),
            {"phone_number": "+233551234567", "password": "WrongPassword!"},
        )
        assert response.status_code == 200

    distributor.refresh_from_db()
    assert distributor.locked_until is not None
    assert distributor.locked_until > timezone.now()

    # Even the correct password is rejected while locked.
    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session

    # Once the lock window has passed, login works again.
    distributor.locked_until = timezone.now() - timedelta(seconds=1)
    distributor.save(update_fields=["locked_until"])

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    assert response.status_code == 302
    assert int(client.session["_auth_user_id"]) == distributor.user.id


@pytest.mark.django_db
def test_successful_login_resets_the_failed_attempt_counter(client):
    distributor = _create_verified_distributor()

    client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "WrongPassword!"},
    )
    distributor.refresh_from_db()
    assert distributor.failed_login_attempts == 1

    client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    distributor.refresh_from_db()
    assert distributor.failed_login_attempts == 0


@pytest.mark.django_db
def test_distributor_login_page_has_no_google_login_option(client):
    response = client.get(reverse("distributors:login"))

    # "google" alone would also match the Google Fonts <link> tags every page
    # uses — check specifically for a social-login button, not just the word.
    content = response.content.lower()
    assert b"continue with google" not in content
    assert b"provider_login_url" not in content
    assert b"/accounts/google/" not in content


@pytest.mark.django_db
def test_distributor_password_reset_via_otp(client):
    distributor = _create_verified_distributor()

    client.post(
        reverse("distributors:forgot_password"), {"phone_number": "+233551234567"}
    )

    assert fake_outbox
    sent_message = fake_outbox[-1]["message"]
    code = "".join(ch for ch in sent_message if ch.isdigit())[:6]

    verify_response = client.post(reverse("distributors:verify_otp"), {"code": code})
    assert verify_response.status_code == 302

    reset_response = client.post(
        reverse("distributors:set_new_password"),
        {"password1": "BrandNewPassw0rd!", "password2": "BrandNewPassw0rd!"},
    )
    assert reset_response.status_code == 302

    distributor.user.refresh_from_db()
    assert distributor.user.check_password("BrandNewPassw0rd!")
