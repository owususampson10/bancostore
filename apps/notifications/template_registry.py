"""Task 48a. Single source of truth for which {{placeholder}} names each
NotificationTemplate.Key supports -- used to (1) show an admin which
variables are available while editing a template, and (2) reject a
template body/subject that references an undeclared placeholder, so a
typo or a copy-pasted wrong variable name is caught at save time, not
silently left as literal '{{typo}}' text in a real customer-facing send.
"""

from .models import NotificationTemplate

Key = NotificationTemplate.Key

PLACEHOLDERS_BY_KEY: dict[str, list[str]] = {
    Key.OTP_CODE: ["code", "expiry_minutes"],
    Key.WITHDRAWAL_APPROVED: ["net_amount", "withdrawal_day"],
    Key.WITHDRAWAL_REJECTED: ["amount", "reason"],
    Key.WITHDRAWAL_PAID: ["net_amount"],
    Key.WITHDRAWAL_REVERSED: ["net_amount"],
    Key.KYC_APPROVED: [],
    Key.KYC_REJECTED: ["reason"],
}
