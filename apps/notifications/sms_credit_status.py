"""Task 66. The last known SMS credit balance, and whether the admin portal
should show a banner about it.

The banner is amber while credit is below SMS_LOW_CREDIT_THRESHOLD and red
once it has run out (both user-confirmed). It reads the stored balance only,
never mNotify, so showing it costs one small query per admin page.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from constance import config

from .models import SmsCreditStatus

logger = logging.getLogger(__name__)

STATUS_PK = 1

# Session key holding the shortage_id of the banner an admin closed. Shared
# by the template tag that hides it and the view that records the close.
BANNER_DISMISSED_SESSION_KEY = "sms_credit_banner_dismissed"


@dataclass(frozen=True)
class CreditBanner:
    kind: str  # "low" or "out"
    credits: int
    # Identifies this shortage, and whether it is low or out, so a banner the
    # admin closed stays closed for exactly this situation and no other.
    shortage_id: str


def _shortage_kind(credits, threshold):
    if credits is None:
        return None
    if credits <= 0:
        return "out"
    if threshold and credits < threshold:
        return "low"
    return None


def record_sms_credit(credits: int) -> None:
    """Store a freshly read balance. Never raises: this runs inside failing
    SMS sends and the hourly job, and must never break either."""
    try:
        now = timezone.now()
        status, _ = SmsCreditStatus.objects.update_or_create(
            pk=STATUS_PK, defaults={"credits": credits, "checked_at": now}
        )
        short = _shortage_kind(credits, config.SMS_LOW_CREDIT_THRESHOLD or 0)
        if short and status.shortage_started_at is None:
            status.shortage_started_at = now
            status.save(update_fields=["shortage_started_at"])
        elif not short and status.shortage_started_at is not None:
            status.shortage_started_at = None
            status.save(update_fields=["shortage_started_at"])
    except Exception:
        logger.exception("record_sms_credit: could not store the SMS credit balance")


def credit_banner() -> CreditBanner | None:
    """The banner to show, or None when credit is fine or never checked."""
    status = SmsCreditStatus.objects.filter(pk=STATUS_PK).first()
    if status is None:
        return None
    kind = _shortage_kind(status.credits, config.SMS_LOW_CREDIT_THRESHOLD or 0)
    if kind is None:
        return None
    # An admin raising the warning level can make an existing balance "low"
    # before any check has marked a shortage; fall back to the check time.
    started = status.shortage_started_at or status.checked_at
    return CreditBanner(
        kind=kind,
        credits=status.credits,
        shortage_id=f"{kind}:{started.isoformat() if started else ''}",
    )
