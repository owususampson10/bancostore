import logging
import secrets
from datetime import timedelta

from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from constance import config

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .email import get_sender_email
from .models import NotificationTemplate, OTPCode
from .rendering import render_or_default
from .sms import SmsSendError, send_sms

logger = logging.getLogger(__name__)

# Task 63b. Only a password reset may fall back to email. The "registration"
# code exists to prove the distributor holds that phone number, and a code
# delivered by email proves nothing about the phone (user-confirmed
# 2026-09-17).
EMAIL_FALLBACK_PURPOSES = frozenset({"password_reset"})


class OtpDeliveryFailed(Exception):
    """The code was created but could not be delivered by any allowed
    route. Callers show a friendly message instead of an error page."""


def generate_otp(
    phone_number: str, *, purpose: str, fallback_email: str = ""
) -> OTPCode:
    """Creates a code and sends it by SMS.

    If the SMS can't be sent (e.g. mNotify is out of credit), a password
    reset code goes to `fallback_email` instead, when there is one. Anything
    else raises OtpDeliveryFailed. The returned code's `delivered_via` says
    which route worked ("sms" or "email").
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    otp = OTPCode.objects.create(
        phone_number=phone_number,
        purpose=purpose,
        code=code,
        expires_at=timezone.now() + timedelta(minutes=config.OTP_CODE_EXPIRY_MINUTES),
    )
    # Task 48b: admin-editable wording, falling back to this exact
    # hardcoded default if the NotificationTemplate row is ever missing
    # -- an OTP send must never fail because of a template lookup.
    message = render_or_default(
        NotificationTemplate.Key.OTP_CODE,
        {"code": code, "expiry_minutes": config.OTP_CODE_EXPIRY_MINUTES},
        default_body=(
            "Your Bancostore verification code is {{code}}. It expires in "
            "{{expiry_minutes}} minutes."
        ),
    )
    try:
        send_sms(
            phone_number,
            message,
            sms_type="otp",
        )
    except SmsSendError:
        # No phone number in the log line: it would tie a person to a
        # failed login or reset attempt for anyone reading the logs.
        logger.exception("generate_otp: SMS failed for purpose=%s", purpose)
        fallback_email = (fallback_email or "").strip()
        if purpose not in EMAIL_FALLBACK_PURPOSES or not fallback_email:
            raise OtpDeliveryFailed(f"could not deliver the {purpose} code") from None
        _send_otp_email(fallback_email, code)
        otp.delivered_via = "email"
        return otp

    otp.delivered_via = "sms"
    return otp


def _send_otp_email(email: str, code: str) -> None:
    try:
        send_mail(
            subject="Your Bancostore password reset code",
            message=(
                f"Your Bancostore password reset code is {code}. It expires in "
                f"{config.OTP_CODE_EXPIRY_MINUTES} minutes.\n\n"
                "We sent it by email because we couldn't reach your phone by "
                "text message.\n\n"
                "If you didn't ask to reset your password, you can ignore this "
                "email -- your password has not changed."
            ),
            from_email=get_sender_email(),
            recipient_list=[email],
        )
    except Exception:
        logger.exception("generate_otp: the email fallback failed too")
        raise OtpDeliveryFailed("could not deliver the password reset code") from None


def verify_otp(phone_number: str, *, purpose: str, submitted_code: str) -> bool:
    """The attempts read-check-increment-save is wrapped in
    select_for_update() + retry_on_lock_contention() — a real
    multi-threaded test reproduced a lost-update race here under
    concurrent wrong guesses before this fix (see tests/unit/notifications/
    test_otp.py::test_concurrent_wrong_guesses_do_not_exceed_max_attempts):
    without locking, several concurrent guesses could each read the same
    stale attempts count and all increment past OTP_MAX_ATTEMPTS, letting
    an attacker exceed the intended guess budget on a 6-digit code."""
    unlocked_otp = (
        OTPCode.objects.filter(
            phone_number=phone_number, purpose=purpose, verified_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if unlocked_otp is None:
        return False
    if unlocked_otp.expires_at < timezone.now():
        return False

    def _attempt():
        with transaction.atomic():
            otp = select_for_update_nowait_if_supported(OTPCode.objects).get(
                pk=unlocked_otp.pk
            )
            if otp.attempts >= config.OTP_MAX_ATTEMPTS:
                return False

            otp.attempts += 1
            if not secrets.compare_digest(otp.code, submitted_code):
                otp.save(update_fields=["attempts"])
                return False

            otp.verified_at = timezone.now()
            otp.save(update_fields=["attempts", "verified_at"])
            return True

    return retry_on_lock_contention(_attempt)
