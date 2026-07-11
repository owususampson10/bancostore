from datetime import timedelta

from django.utils import timezone

import pytest
from constance import config

from apps.notifications.otp import generate_otp, verify_otp
from apps.notifications.sms import fake_outbox


@pytest.mark.django_db
def test_generate_otp_creates_a_six_digit_code_with_expiry():
    otp = generate_otp("+233241234567", purpose="registration")

    assert len(otp.code) == 6
    assert otp.code.isdigit()
    assert otp.phone_number == "+233241234567"
    assert otp.purpose == "registration"
    expected_expiry = otp.created_at + timedelta(minutes=config.OTP_CODE_EXPIRY_MINUTES)
    assert abs((otp.expires_at - expected_expiry).total_seconds()) < 1


@pytest.mark.django_db
def test_generate_otp_sends_an_sms_with_the_code():
    otp = generate_otp("+233241234567", purpose="registration")

    assert fake_outbox, "no SMS was sent via the fake sender"
    assert fake_outbox[-1]["phone_number"] == "+233241234567"
    assert otp.code in fake_outbox[-1]["message"]


@pytest.mark.django_db
def test_verify_otp_succeeds_with_the_correct_code():
    otp = generate_otp("+233241234567", purpose="registration")

    assert (
        verify_otp("+233241234567", purpose="registration", submitted_code=otp.code)
        is True
    )

    otp.refresh_from_db()
    assert otp.verified_at is not None


@pytest.mark.django_db
def test_verify_otp_fails_with_the_wrong_code():
    generate_otp("+233241234567", purpose="registration")

    assert (
        verify_otp("+233241234567", purpose="registration", submitted_code="000000")
        is False
    )


@pytest.mark.django_db
def test_verify_otp_fails_after_expiry():
    otp = generate_otp("+233241234567", purpose="registration")
    otp.expires_at = timezone.now() - timedelta(seconds=1)
    otp.save()

    assert (
        verify_otp("+233241234567", purpose="registration", submitted_code=otp.code)
        is False
    )


@pytest.mark.django_db
def test_verify_otp_fails_after_max_attempts():
    otp = generate_otp("+233241234567", purpose="registration")

    for _ in range(config.OTP_MAX_ATTEMPTS):
        assert (
            verify_otp("+233241234567", purpose="registration", submitted_code="000000")
            is False
        )

    # Even the correct code is rejected once attempts are exhausted.
    assert (
        verify_otp("+233241234567", purpose="registration", submitted_code=otp.code)
        is False
    )


@pytest.mark.django_db
def test_verify_otp_only_matches_the_right_purpose():
    otp = generate_otp("+233241234567", purpose="registration")

    assert (
        verify_otp("+233241234567", purpose="password_reset", submitted_code=otp.code)
        is False
    )
