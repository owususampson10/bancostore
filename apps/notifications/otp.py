import secrets
from datetime import timedelta

from django.utils import timezone

from constance import config

from .models import OTPCode
from .sms import send_sms


def generate_otp(phone_number: str, *, purpose: str) -> OTPCode:
    code = f"{secrets.randbelow(1_000_000):06d}"
    otp = OTPCode.objects.create(
        phone_number=phone_number,
        purpose=purpose,
        code=code,
        expires_at=timezone.now() + timedelta(minutes=config.OTP_CODE_EXPIRY_MINUTES),
    )
    send_sms(
        phone_number,
        f"Your Bancostore verification code is {code}. It expires in "
        f"{config.OTP_CODE_EXPIRY_MINUTES} minutes.",
        sms_type="otp",
    )
    return otp


def verify_otp(phone_number: str, *, purpose: str, submitted_code: str) -> bool:
    otp = (
        OTPCode.objects.filter(
            phone_number=phone_number, purpose=purpose, verified_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if otp is None:
        return False
    if otp.expires_at < timezone.now():
        return False
    if otp.attempts >= config.OTP_MAX_ATTEMPTS:
        return False

    otp.attempts += 1
    if otp.code != submitted_code:
        otp.save(update_fields=["attempts"])
        return False

    otp.verified_at = timezone.now()
    otp.save(update_fields=["attempts", "verified_at"])
    return True
