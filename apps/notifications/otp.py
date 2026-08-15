import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from constance import config

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import NotificationTemplate, OTPCode
from .rendering import render_or_default
from .sms import send_sms


def generate_otp(phone_number: str, *, purpose: str) -> OTPCode:
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
    send_sms(
        phone_number,
        message,
        sms_type="otp",
    )
    return otp


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
