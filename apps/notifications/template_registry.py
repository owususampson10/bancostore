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
    Key.BINARY_BONUS_CREDITED: ["amount"],
    Key.DOWNLINE_JOINED: ["name"],
    Key.DIRECT_REFERRAL_BONUS_CREDITED_SMS: ["amount", "referred_name"],
    Key.DIRECT_REFERRAL_BONUS_CREDITED_INAPP: ["amount", "referred_name"],
    Key.PV_EXPIRING: ["pv", "expiry_date"],
    Key.ORDER_STATUS_UPDATE_SMS: ["reference", "status"],
    Key.ORDER_STATUS_UPDATE_EMAIL: ["reference", "status"],
}

# CodeRabbit finding (PR #79): PLACEHOLDERS_BY_KEY above only says which
# placeholders are ALLOWED -- nothing stopped an admin from saving an
# OTP_CODE template with no {{code}} in it at all, which would silently
# break every registration/password-reset OTP send afterward (the SMS
# would go out with no code in it, and nothing anywhere would error).
# Every other key's placeholders are informational, not load-bearing --
# e.g. a withdrawal-rejected template missing {{reason}} is a less
# helpful message, not a broken one -- so this is deliberately scoped to
# just the one case where omitting a placeholder makes the notification
# functionally useless, not a general "required placeholders" system.
REQUIRED_PLACEHOLDERS_BY_KEY: dict[str, list[str]] = {
    Key.OTP_CODE: ["code"],
}
