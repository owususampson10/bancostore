from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.utils import timezone

import pytest
from constance import config

from apps.notifications.models import NotificationTemplate
from apps.notifications.otp import OtpDeliveryFailed, generate_otp, verify_otp
from apps.notifications.sms import SmsOutOfCredit, SmsSendError, fake_outbox


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
def test_generate_otp_uses_the_live_admin_edited_template_wording():
    """Task 48b acceptance criteria: editing a template's wording from
    admin_portal changes the next real send -- not just that the edit
    saves. OTP_CODE is pre-seeded by migration 0006_seed_notification_
    templates, so this updates that already-existing row."""
    NotificationTemplate.objects.filter(key=NotificationTemplate.Key.OTP_CODE).update(
        body="Custom wording, code {{code}}, valid {{expiry_minutes}} min."
    )

    otp = generate_otp("+233241234567", purpose="registration")

    message = fake_outbox[-1]["message"]
    assert message == (
        f"Custom wording, code {otp.code}, valid "
        f"{config.OTP_CODE_EXPIRY_MINUTES} min."
    )


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


@pytest.mark.django_db(transaction=True)
def test_concurrent_wrong_guesses_do_not_exceed_max_attempts():
    """Regression test: otp.attempts was read, incremented in memory, and
    saved with no locking — the same lost-update race already fixed for
    admin/distributor login counters and stock decrement, just missed here.
    Fire more concurrent wrong guesses than OTP_MAX_ATTEMPTS allows and
    confirm the recorded attempt count is exact, not undercounted from
    lost increments."""
    import threading

    from django.db import connection

    from apps.notifications.models import OTPCode

    generate_otp("+233241234567", purpose="registration")

    def guess():
        try:
            verify_otp("+233241234567", purpose="registration", submitted_code="000000")
        finally:
            connection.close()

    threads = [threading.Thread(target=guess) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    otp = OTPCode.objects.get(phone_number="+233241234567", purpose="registration")
    # With real locking, each of the 10 concurrent guesses is serialized:
    # the first OTP_MAX_ATTEMPTS see the cap not yet reached and increment;
    # the rest see it already reached and bail without incrementing. A lost
    # -update race would let more than OTP_MAX_ATTEMPTS guesses each read a
    # stale count and increment past the intended cap.
    assert otp.attempts == config.OTP_MAX_ATTEMPTS


# --- Task 63b: an SMS that can't be sent ------------------------------------
#
# mNotify ran out of credit on 2026-09-17. generate_otp let the send failure
# escape, so a distributor logging in or resetting their password got an
# error page. Now: password reset falls back to email when the account has
# one (user-confirmed); the first-login phone check never does, since a code
# delivered by email proves nothing about the phone.


_SMS_DOWN = patch(
    "apps.notifications.otp.send_sms", side_effect=SmsOutOfCredit("no credit")
)


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.mark.django_db
def test_a_failed_sms_for_the_phone_check_is_reported_not_raised_raw(locmem):
    with _SMS_DOWN, pytest.raises(OtpDeliveryFailed):
        generate_otp(
            "+233241234567", purpose="registration", fallback_email="kofi@example.com"
        )

    assert mail.outbox == []  # never by email: email can't prove the phone


@pytest.mark.django_db
def test_a_failed_sms_for_password_reset_falls_back_to_email(locmem):
    with _SMS_DOWN:
        otp = generate_otp(
            "+233241234567", purpose="password_reset", fallback_email="kofi@example.com"
        )

    (message,) = mail.outbox
    assert message.to == ["kofi@example.com"]
    assert otp.code in message.body
    assert otp.delivered_via == "email"


@pytest.mark.django_db
def test_a_failed_sms_for_password_reset_without_an_email_is_reported(locmem):
    with _SMS_DOWN, pytest.raises(OtpDeliveryFailed):
        generate_otp("+233241234567", purpose="password_reset", fallback_email="")

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_failed_email_fallback_is_reported_too(locmem):
    with (
        _SMS_DOWN,
        patch("apps.notifications.otp.send_mail", side_effect=OSError("smtp down")),
        pytest.raises(OtpDeliveryFailed),
    ):
        generate_otp(
            "+233241234567", purpose="password_reset", fallback_email="kofi@example.com"
        )


@pytest.mark.django_db
def test_a_delivered_sms_never_also_sends_an_email(locmem):
    otp = generate_otp(
        "+233241234567", purpose="password_reset", fallback_email="kofi@example.com"
    )

    assert mail.outbox == []
    assert otp.delivered_via == "sms"


@pytest.mark.django_db
def test_any_sms_failure_counts_not_only_running_out_of_credit(locmem):
    with (
        patch("apps.notifications.otp.send_sms", side_effect=SmsSendError("HTTP 500")),
        pytest.raises(OtpDeliveryFailed),
    ):
        generate_otp("+233241234567", purpose="registration")


@pytest.mark.django_db
def test_a_code_that_could_not_be_delivered_does_not_replace_the_last_one(locmem):
    """CodeRabbit (PR #94). verify_otp checks the NEWEST code. A resend
    that fails must not leave an undelivered code on top, or the code the
    distributor already received stops working."""
    delivered = generate_otp("+233241234567", purpose="registration")

    with _SMS_DOWN, pytest.raises(OtpDeliveryFailed):
        generate_otp("+233241234567", purpose="registration")

    assert verify_otp(
        "+233241234567", purpose="registration", submitted_code=delivered.code
    )


@pytest.mark.django_db
def test_a_failed_email_fallback_does_not_replace_the_last_code(locmem):
    delivered = generate_otp("+233241234567", purpose="password_reset")

    with (
        _SMS_DOWN,
        patch("apps.notifications.otp.send_mail", side_effect=OSError("smtp down")),
        pytest.raises(OtpDeliveryFailed),
    ):
        generate_otp(
            "+233241234567", purpose="password_reset", fallback_email="kofi@example.com"
        )

    assert verify_otp(
        "+233241234567", purpose="password_reset", submitted_code=delivered.code
    )
