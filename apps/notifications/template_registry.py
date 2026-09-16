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
    # Task 60. {{items}} is pre-rendered server-side into one string by
    # apps.orders.receipts.render_items_block -- the renderer has no loop
    # construct, deliberately (see apps.notifications.rendering's module
    # docstring). An admin can move the block and reword around it, but
    # not restructure the per-line format.
    # Task 61a/61b: admin-facing. customer_phone/customer_email exist
    # only here -- a customer's own receipt has no reason to repeat their
    # contact details back at them, but an admin alert is useless without
    # them, since acting on an order means reaching the buyer.
    Key.ADMIN_NEW_ORDER_EMAIL: [
        "reference",
        "order_date",
        "total",
        "customer_name",
        "customer_phone",
        "customer_email",
        "items",
        "delivery_details",
    ],
    Key.ADMIN_NEW_ORDER_SMS: ["reference", "total", "customer_name"],
    Key.ORDER_CONFIRMED_SMS: ["total", "reference"],
    Key.ORDER_CONFIRMED_EMAIL: [
        "customer_name",
        "reference",
        "order_date",
        "items",
        "subtotal",
        "discount_amount",
        "delivery_fee",
        "total",
        "delivery_details",
    ],
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
