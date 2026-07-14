"""
django-constance config: every admin-editable business rule for the MVP-relevant
Section 13 categories (13.1-13.4, 13.9, 13.13-13.15). Defaults are taken from
SPEC.md / docs/Bancostore_Features_and_Workflow_v4.docx Section 13 and Section 15
(Key Rules Summary).

Sections 13.5-13.8 and 13.10-13.12 are out of MVP scope per SPEC.md and are not
seeded here — see SPEC.md "In scope (MVP)".
"""

from decimal import Decimal

AUTHENTICATION_SETTINGS = {
    "GOOGLE_LOGIN_CUSTOMERS_ENABLED": (
        True,
        "Enable Google one-click login for regular customers",
    ),
    "GOOGLE_LOGIN_DISTRIBUTORS_ENABLED": (
        False,
        "Enable Google login for distributors (recommended: off)",
    ),
    "DISTRIBUTOR_LOGIN_METHOD": (
        "phone",
        "Whether distributors log in with a phone number or email",
    ),
    "OTP_CODE_EXPIRY_MINUTES": (
        15,
        "Minutes before a one-time SMS code expires — raised from the original "
        "spec value of 5 after real mNotify delivery testing (Task 5) showed "
        "5 minutes often wasn't enough",
    ),
    "OTP_MAX_ATTEMPTS": (
        3,
        "How many times a user can try an OTP before it is invalidated",
    ),
    "MAX_FAILED_LOGIN_ATTEMPTS": (
        5,
        "Wrong password attempts before an account is temporarily locked",
    ),
    "ACCOUNT_LOCKOUT_DURATION_MINUTES": (
        30,
        "How long an account stays locked after too many failed login attempts",
    ),
    "LOCKOUT_ALERT_EMAIL": (
        "",
        "Email address notified when an account is locked due to failed attempts",
    ),
    "SESSION_TIMEOUT_MINUTES": (
        30,
        "How long before an inactive user is automatically logged out",
    ),
    "ADMIN_SESSION_TIMEOUT_MINUTES": (
        15,
        "How long before an inactive admin is automatically logged out",
    ),
    "ADMIN_2FA_ENABLED": (
        True,
        "Two-Factor Authentication for admin accounts — mandatory, must stay on "
        "(see SPEC.md Boundaries)",
    ),
    "ADMIN_2FA_METHOD": (
        "sms",
        "How the 2FA code is delivered to admin: sms or authenticator app",
    ),
    "MIN_PASSWORD_LENGTH": (
        8,
        "Minimum number of characters required for all passwords",
    ),
    "PASSWORD_COMPLEXITY_ENABLED": (
        True,
        "Whether passwords must contain numbers and capital letters",
    ),
    "PASSWORD_RESET_METHOD_CUSTOMERS": (
        "email_or_sms",
        "How customers reset their password: email link or SMS OTP",
    ),
    "PASSWORD_RESET_METHOD_DISTRIBUTORS": (
        "sms_otp",
        "How distributors reset their password",
    ),
    "PASSWORD_RESET_EXPIRY_MINUTES": (
        10,
        "How long a password reset link or OTP remains valid",
    ),
}

COMMISSION_AND_BONUS_SETTINGS = {
    "BINARY_BONUS_RATE": (
        Decimal("7.5"),
        "Percentage applied to the weak leg PV for every binary bonus calculation",
    ),
    "WEEKLY_BINARY_BONUS_CAP": (
        Decimal("50000"),
        "Maximum a distributor can earn per week from binary bonus (GHS)",
    ),
    "BINARY_BONUS_INTERVAL_MINUTES": (
        10,
        "How often the background job calculates binary bonuses",
    ),
    "DIRECT_REFERRAL_BONUS_RATE": (
        Decimal("10"),
        "Percentage earned when a personally sponsored distributor makes a purchase",
    ),
    "MATCHING_BONUS_RATE": (
        Decimal("5"),
        "Percentage earned on downline binary earnings",
    ),
    "MATCHING_BONUS_DEPTH_BRONZE": (
        3,
        "How many levels deep matching bonus goes for Bronze rank distributors",
    ),
    "MATCHING_BONUS_DEPTH_SILVER": (
        0,
        "How many levels deep matching bonus goes for Silver rank distributors "
        "(0 = unlimited)",
    ),
    "MIN_MONTHLY_PERSONAL_PV": (
        100,
        "Minimum PV a distributor must generate monthly to stay bonus-eligible",
    ),
    "PV_CARRY_FORWARD_EXPIRY_DAYS": (
        180,
        "How many days unused strong-leg PV is held before it expires",
    ),
    "PV_EXPIRY_WARNING_DAYS": (
        14,
        "How many days before expiry the system warns the distributor",
    ),
}

REGISTRATION_AND_MEMBERSHIP_SETTINGS = {
    "REGISTRATION_FEE": (
        Decimal("100"),
        "One-time fee to join as a distributor (GHS)",
    ),
    "REGISTRATION_FEE_REFUNDABLE": (
        False,
        "Whether the registration fee is included in a cooling-off refund",
    ),
    "STARTER_PACK_A_PRICE": (
        Decimal("1500"),
        "Price of Starter Pack A (GHS)",
    ),
    "STARTER_PACK_A_PV": (
        500,
        "PV allocated to a Starter Pack A purchase",
    ),
    "STARTER_PACK_A_RANK": (
        "bronze",
        "Rank assigned on Starter Pack A purchase",
    ),
    "STARTER_PACK_B_PRICE": (
        Decimal("2000"),
        "Price of Starter Pack B (GHS)",
    ),
    "STARTER_PACK_B_PV": (
        1000,
        "PV allocated to a Starter Pack B purchase",
    ),
    "STARTER_PACK_B_RANK": (
        "silver",
        "Rank assigned on Starter Pack B purchase",
    ),
    "COOLING_OFF_PERIOD_DAYS": (
        7,
        "Days a new distributor has to cancel and get a refund",
    ),
    "COOLING_OFF_REFUND_DEDUCTION_RATE": (
        Decimal("10"),
        "Processing fee percentage deducted from a cooling-off refund",
    ),
}

WITHDRAWAL_AND_PAYOUT_SETTINGS = {
    "WITHDRAWAL_FREQUENCY": (
        "weekly",
        "How often distributors can request a withdrawal",
    ),
    "WITHDRAWAL_DAY": (
        "",
        "Which day of the week withdrawals are processed — set by admin "
        "(SPEC.md Open Question #5)",
    ),
    "MIN_WITHDRAWAL_AMOUNT": (
        Decimal("0"),
        "Minimum balance required before a withdrawal can be requested (GHS) — "
        "placeholder, needs an admin decision (SPEC.md Open Question #5)",
    ),
    "MAX_WITHDRAWAL_AMOUNT": (
        Decimal("0"),
        "Cap on how much can be withdrawn in a single request (GHS) — "
        "placeholder, needs an admin decision (SPEC.md Open Question #5)",
    ),
    "WITHHOLDING_TAX_RATE": (
        Decimal("1"),
        "Tax percentage deducted from every withdrawal and sent to GRA",
    ),
    "ESCROW_RESERVE_RATE": (
        Decimal("5"),
        "Percentage of all product revenue held at GCB Bank",
    ),
    "AUTO_APPROVE_WITHDRAWALS_ENABLED": (
        False,
        "Whether small withdrawals are approved automatically",
    ),
    "AUTO_APPROVE_WITHDRAWAL_THRESHOLD": (
        Decimal("0"),
        "Withdrawals below this amount are auto-approved if the toggle is on (GHS)",
    ),
}

KYC_SETTINGS = {
    "KYC_REQUIRED": (
        True,
        "Must KYC be complete before a distributor's first withdrawal",
    ),
    "KYC_REJECTION_REASONS": (
        "",
        "Comma-separated pre-set list of rejection reasons admin selects from",
    ),
}

IR_ID_NUMBER_SETTINGS = {
    "IR_ID_PREFIX": (
        "IR",
        "Prefix before the number in every IR ID",
    ),
    "IR_ID_STARTING_NUMBER": (
        1,
        "The number the system starts from for the first IR ID generated",
    ),
    "IR_ID_NUMBER_OF_DIGITS": (
        5,
        "How many digits the ID number should have",
    ),
}

PAYMENT_GATEWAY_SETTINGS = {
    # PAYSTACK_PUBLIC_KEY / PAYSTACK_SECRET_KEY deliberately do NOT live here.
    # This dict is django-constance -- plaintext in the database, fine for
    # business-rule copy but not for real API keys. They're environment
    # variables instead (bancostore/settings.py, matching MNOTIFY_API_KEY /
    # EMAIL_HOST_PASSWORD) -- fixed here, when Paystack work actually
    # started, per the standing note in tasks/todo.md's known issues (added
    # before any Paystack code existed, specifically so this wasn't shipped
    # silently once the "done-looking" admin scaffolding tempted someone to
    # just fill in the empty constance field instead).
    "PAYSTACK_PAYMENT_CHANNELS": (
        "card,mobile_money",
        "Comma-separated payment methods to enable",
    ),
    "PAYMENT_GATEWAY_MODE": (
        "test",
        "Toggle between test and live mode without code changes",
    ),
    "PAYMENT_FAILURE_MESSAGE": (
        "Your payment could not be processed. Please try again.",
        "Message shown to customer if payment fails",
    ),
    "PAYMENT_SUCCESS_REDIRECT": (
        "order_confirmation",
        "Page shown to customer after successful payment",
    ),
}

GENERAL_PLATFORM_SETTINGS = {
    "PLATFORM_NAME": (
        "Bancostore",
        "The name displayed across the entire platform",
    ),
    "PLATFORM_LOGO": (
        "",
        "Path/URL to the logo shown in the header and on receipts",
    ),
    "PLATFORM_FAVICON": (
        "",
        "Path/URL to the small icon shown in the browser tab",
    ),
    "PRIMARY_COLOUR": (
        "",
        "Main brand colour used across the platform (hex code)",
    ),
    "CONTACT_PHONE_NUMBER": (
        "",
        "Shown on the contact page and receipts",
    ),
    "CONTACT_EMAIL_ADDRESS": (
        "",
        "Shown on the contact page",
    ),
    "WHATSAPP_SUPPORT_NUMBER": (
        "",
        "WhatsApp number customers can click to chat with support",
    ),
    "PHYSICAL_ADDRESS": (
        "",
        "Business address shown on the contact page and invoices",
    ),
    "SOCIAL_MEDIA_LINKS": (
        "",
        "Facebook, Instagram, TikTok, Twitter links for the footer",
    ),
    "CURRENCY": (
        "GHS",
        "Currency used across the platform",
    ),
    "CURRENCY_SYMBOL": (
        "GHS",
        "Symbol displayed with all prices",
    ),
    "MAINTENANCE_MODE_ENABLED": (
        False,
        "Put the whole site into maintenance mode with a custom message",
    ),
    "TERMS_AND_CONDITIONS_TEXT": (
        "",
        "Editable legal text — no developer needed to update",
    ),
    "PRIVACY_POLICY_TEXT": (
        "",
        "Editable privacy policy — no developer needed to update",
    ),
    "REFUND_RETURN_POLICY_TEXT": (
        "",
        "Editable refund policy — no developer needed to update",
    ),
}

CONSTANCE_CONFIG = {
    **AUTHENTICATION_SETTINGS,
    **COMMISSION_AND_BONUS_SETTINGS,
    **REGISTRATION_AND_MEMBERSHIP_SETTINGS,
    **WITHDRAWAL_AND_PAYOUT_SETTINGS,
    **KYC_SETTINGS,
    **IR_ID_NUMBER_SETTINGS,
    **PAYMENT_GATEWAY_SETTINGS,
    **GENERAL_PLATFORM_SETTINGS,
}

CONSTANCE_CONFIG_FIELDSETS = {
    "Authentication Settings": tuple(AUTHENTICATION_SETTINGS),
    "Commission & Bonus Settings": tuple(COMMISSION_AND_BONUS_SETTINGS),
    "Registration & Membership Settings": tuple(REGISTRATION_AND_MEMBERSHIP_SETTINGS),
    "Withdrawal & Payout Settings": tuple(WITHDRAWAL_AND_PAYOUT_SETTINGS),
    "KYC Settings": tuple(KYC_SETTINGS),
    "IR ID Number Settings": tuple(IR_ID_NUMBER_SETTINGS),
    "Payment Gateway Settings": tuple(PAYMENT_GATEWAY_SETTINGS),
    "General Platform Settings": tuple(GENERAL_PLATFORM_SETTINGS),
}
