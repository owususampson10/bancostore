"""Task 63b. When the verification SMS can't be sent, distributors see a
friendly page, never an error page, and password reset can fall back to
email.

Before this, mNotify running out of credit (2026-09-17) turned the login,
forgot-password and resend-code pages into server errors, because
generate_otp let the send failure escape.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.notifications.sms import SmsOutOfCredit, fake_outbox

User = get_user_model()
PASSWORD = "S3cure-Passw0rd!"


def _distributor(phone="+233241234567", email="", phone_verified=False):
    user = User.objects.create_user(username=phone, password=PASSWORD, email=email)
    group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(group)
    return Distributor.objects.create(
        user=user, phone_number=phone, phone_verified=phone_verified
    )


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def sms_down():
    with patch(
        "apps.notifications.otp.send_sms", side_effect=SmsOutOfCredit("no credit")
    ):
        yield


def _set_otp_session(client, phone, purpose):
    session = client.session
    session["otp_phone_number"] = phone
    session["otp_purpose"] = purpose
    session.save()


# --- First login: the phone check -------------------------------------------


@pytest.mark.django_db
def test_login_shows_a_friendly_message_when_the_code_cannot_be_texted(
    client, locmem, sms_down
):
    _distributor(email="kofi@example.com")

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233241234567", "password": PASSWORD},
    )

    assert response.status_code == 200
    assert "couldn&#x27;t send your verification code" in response.content.decode()
    assert "otp_phone_number" not in client.session
    assert mail.outbox == []  # the phone check never falls back to email


@pytest.mark.django_db
def test_resending_the_phone_check_code_explains_a_failure(client, sms_down):
    _distributor()
    _set_otp_session(client, "+233241234567", "registration")

    response = client.post(reverse("distributors:resend_otp"), follow=True)

    assert response.status_code == 200
    # Shown once, by the site layout's own message area -- the page must not
    # add a second copy.
    assert response.content.decode().count("couldn&#x27;t send a new code") == 1


# --- Password reset ---------------------------------------------------------


@pytest.mark.django_db
def test_forgot_password_emails_the_code_when_the_text_fails(client, locmem, sms_down):
    _distributor(email="kofi@example.com", phone_verified=True)

    response = client.post(
        reverse("distributors:forgot_password"), {"phone_number": "+233241234567"}
    )

    assert response.status_code == 302
    assert response.url == reverse("distributors:verify_otp")
    (message,) = mail.outbox
    assert message.to == ["kofi@example.com"]


@pytest.mark.django_db
def test_forgot_password_looks_the_same_whatever_happened(client, locmem, sms_down):
    """No page may reveal whether a number has an account, or whether that
    account has an email address. All three cases redirect identically."""
    _distributor(phone="+233241111111", email="kofi@example.com", phone_verified=True)
    _distributor(phone="+233242222222", email="", phone_verified=True)

    urls = []
    for phone in ("+233241111111", "+233242222222", "+233243333333"):
        response = client.post(
            reverse("distributors:forgot_password"), {"phone_number": phone}
        )
        assert response.status_code == 302
        urls.append(response.url)

    assert len(set(urls)) == 1


@pytest.mark.django_db
def test_the_reset_code_page_suggests_checking_email(client):
    _set_otp_session(client, "+233241234567", "password_reset")

    response = client.get(reverse("distributors:verify_otp"))

    assert "check your email" in response.content.decode()


@pytest.mark.django_db
def test_the_phone_check_page_does_not_mention_email(client):
    _set_otp_session(client, "+233241234567", "registration")

    response = client.get(reverse("distributors:verify_otp"))

    assert "check your email" not in response.content.decode()


@pytest.mark.django_db
def test_resending_a_reset_code_falls_back_to_email_too(client, locmem, sms_down):
    _distributor(email="kofi@example.com", phone_verified=True)
    _set_otp_session(client, "+233241234567", "password_reset")

    response = client.post(reverse("distributors:resend_otp"))

    assert response.status_code == 302
    (message,) = mail.outbox
    assert message.to == ["kofi@example.com"]


@pytest.mark.django_db
def test_resending_a_reset_code_for_an_unknown_number_sends_nothing(client):
    """forgot_password never sends to a number with no account; resend used
    to, spending a real SMS credit on any number typed into the form."""
    _set_otp_session(client, "+233249999999", "password_reset")

    response = client.post(reverse("distributors:resend_otp"))

    assert response.status_code == 302
    assert fake_outbox == []


@pytest.mark.django_db
def test_resending_a_reset_code_never_reveals_a_failure(client, locmem, sms_down):
    """Unlike the phone check, a reset page can't say "we couldn't send":
    an unknown number never tries to send, so that message would only ever
    appear for real accounts."""
    _distributor(email="", phone_verified=True)
    _set_otp_session(client, "+233241234567", "password_reset")

    response = client.post(reverse("distributors:resend_otp"), follow=True)

    assert "couldn&#x27;t send" not in response.content.decode()
