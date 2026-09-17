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
        "Enable Google one-click login for regular customers. Only shows "
        "the button when a Google app is also configured in Django Admin "
        "(Social Applications) -- turning this on alone won't show a "
        "button with no Google app configured.",
    ),
    "GOOGLE_LOGIN_DISTRIBUTORS_ENABLED": (
        False,
        "Not yet enforced -- distributor login has no Google sign-in option "
        "built yet. Toggling this currently has no effect.",
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
        "authenticator_app",
        "How the 2FA code is delivered to admin. Not yet enforced -- only "
        "authenticator-app (TOTP) 2FA is implemented; no SMS delivery path "
        "exists for admin 2FA.",
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
        "Not yet enforced anywhere. Customer email reset-link expiry is "
        "Django's own static PASSWORD_RESET_TIMEOUT setting (3 days); "
        "distributor SMS-OTP reset uses OTP_CODE_EXPIRY_MINUTES instead. "
        "Neither reads this value.",
    ),
}

COMMISSION_AND_BONUS_SETTINGS = {
    "BINARY_BONUS_RATE": (
        Decimal("7.5"),
        "Percentage applied to the weak leg PV for every binary bonus calculation",
        "percentage_field",
    ),
    "WEEKLY_BINARY_BONUS_CAP": (
        Decimal("50000"),
        "Maximum a distributor can earn per week from binary bonus (GHS)",
        "non_negative_money_field",
    ),
    "BINARY_BONUS_INTERVAL_MINUTES": (
        10,
        "How often the background job calculates binary bonuses",
        "interval_minutes_field",
    ),
    "DIRECT_REFERRAL_BONUS_RATE": (
        Decimal("10"),
        "Percentage earned when a personally sponsored distributor makes a purchase",
    ),
    "MATCHING_BONUS_RATE": (
        Decimal("5"),
        "Percentage earned on downline binary earnings",
        "percentage_field",
    ),
    "MATCHING_BONUS_DEPTH_BRONZE": (
        3,
        "How many levels deep matching bonus goes for Bronze rank distributors",
        "non_negative_depth_field",
    ),
    "MATCHING_BONUS_DEPTH_SILVER": (
        0,
        "How many levels deep matching bonus goes for Silver rank distributors "
        "(0 = unlimited)",
        "non_negative_depth_field",
    ),
    "MATCHING_BONUS_INTERVAL_DAYS": (
        7,
        "How often the background job calculates matching bonuses",
        "interval_days_field",
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
        # Task 16c (doubt-driven-development, pre-implementation review):
        # this was a plain string with no widget until this pass -- exactly
        # the same "admin can type anything" gap WITHDRAWAL_DAY had before
        # its own day_of_week_field fix, except worse here: a mistyped
        # value doesn't just misconfigure one setting, it raises on every
        # single withdrawal submission platform-wide (apps/withdrawal/
        # services.py::WITHDRAWAL_FREQUENCY_DURATIONS has no entry for
        # anything but "weekly"). Only one choice exists today -- the
        # widget still matters, since a bounded ChoiceField can't be
        # fat-fingered the way a free-text field can.
        "How often distributors can request a withdrawal",
        "withdrawal_frequency_field",
    ),
    "WITHDRAWAL_DAY": (
        "friday",
        # SPEC.md Open Question #5, resolved 2026-07-22 by the user: Friday --
        # a common payday convention, and it lands the same week Matching
        # Bonus already pays out on (also weekly).
        "Which day of the week withdrawals are processed",
        "day_of_week_field",
    ),
    "MIN_WITHDRAWAL_AMOUNT": (
        Decimal("100"),
        # Resolved 2026-07-22 (SPEC.md Open Question #5): a round minimum
        # that avoids tiny payout requests without being a high bar for
        # newer distributors.
        "Minimum balance required before a withdrawal can be requested (GHS)",
        "non_negative_money_field",
    ),
    "MAX_WITHDRAWAL_AMOUNT": (
        Decimal("10000"),
        # Resolved 2026-07-22 (SPEC.md Open Question #5): generous enough
        # for a high-performing distributor's weekly earnings, while still
        # bounding the blast radius of a single erroneous/fraudulent
        # request -- same reasoning WEEKLY_BINARY_BONUS_CAP already applies
        # one layer up, at the whole-week rather than single-request level.
        "Cap on how much can be withdrawn in a single request (GHS)",
        "non_negative_money_field",
    ),
    "HIGH_PRIORITY_WITHDRAWAL_THRESHOLD": (
        Decimal("5000"),
        # Task 23 (Admin Portal): the Withdrawal Review queue flags a
        # pending request as "high priority" -- worth a closer look, not
        # a different approval path -- once it's requested above this
        # amount. Deliberately does NOT gate approval itself (no
        # multi-tier admin permission system exists in this codebase;
        # any staff admin can approve any amount) -- it's a display-only
        # nudge. Defaulted to half of MAX_WITHDRAWAL_AMOUNT as a starting
        # point, fully admin-editable like its siblings above.
        "Withdrawal requests above this amount are flagged for extra scrutiny (GHS)",
        "non_negative_money_field",
    ),
    "WITHHOLDING_TAX_RATE": (
        Decimal("1"),
        # Task 16c (code-review-and-quality, 2026-07-23): was a plain
        # 2-tuple with no bound, unlike its percentage siblings
        # BINARY_BONUS_RATE/MATCHING_BONUS_RATE, which both already use
        # percentage_field for exactly this reason. An unbounded rate here
        # could go negative or above 100, producing a negative tax_amount
        # or a net_amount that exceeds the requested amount --
        # apps/withdrawal/services.py::submit_withdrawal_request has its
        # own defensive check for this, but the bounded widget is the
        # first line of defense, closing the gap at its actual source.
        "Tax percentage deducted from every withdrawal and sent to GRA",
        "percentage_field",
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

DELIVERY_SETTINGS = {
    # Task 17a (SPEC.md Open Question #4, resolved 2026-07-24): the source
    # doc's own Section 5.1 worked example, not a guess -- see
    # docs/decisions/0005-checkout-cart-design.md decision 1. All four
    # remain fully admin-editable at runtime; these are just starting
    # values, same treatment as WITHDRAWAL_AND_PAYOUT_SETTINGS's own
    # min/max amounts.
    "DELIVERY_FEE_KUMASI": (
        Decimal("20"),
        "Delivery fee for orders within Kumasi (GHS)",
        "non_negative_money_field",
    ),
    "DELIVERY_FEE_ACCRA": (
        Decimal("50"),
        "Delivery fee for orders within Accra (GHS)",
        "non_negative_money_field",
    ),
    "DELIVERY_FEE_OTHER_REGIONS": (
        Decimal("70"),
        "Delivery fee for orders outside Kumasi and Accra (GHS)",
        "non_negative_money_field",
    ),
    "FREE_DELIVERY_THRESHOLD": (
        Decimal("500"),
        "Orders at or above this subtotal (GHS) get free delivery in any zone",
        "non_negative_money_field",
    ),
}

ORDER_SETTINGS = {
    # Task 18d. Admin-editable cutoff for apps.orders.tasks
    # .auto_cancel_unpaid_orders (ADR-0006 decision 6) -- purely a
    # housekeeping window, not tied to any stock reservation (none exists,
    # per ADR-0005 decision 5) or payment retry logic. 24 hours, matching
    # standard e-commerce abandoned-checkout cleanup convention, confirmed
    # with the user 2026-07-26.
    # Task 61a. Blank = disabled, the same convention as
    # COMPLIANCE_ALERT_EMAIL/LOCKOUT_ALERT_EMAIL above and MNOTIFY_API_KEY
    # -- a store that has not set an address simply gets no alert, rather
    # than the send failing obscurely against an empty recipient. Before
    # this existed a paid order notified the CUSTOMER and nobody else;
    # the admin found out by logging in and looking at a dashboard card.
    "ADMIN_ORDER_ALERT_EMAIL": (
        "",
        "Emailed whenever a customer's payment is confirmed. Leave blank "
        "to disable.",
    ),
    # Task 61b. Same blank-means-disabled convention as the email above.
    # Independent of it on purpose: SMS costs real money per message and
    # email does not, so a store may well want one and not the other.
    "ADMIN_ORDER_ALERT_SMS_NUMBER": (
        "",
        "Texted whenever a customer's payment is confirmed. Costs one SMS "
        "credit per order. Leave blank to disable.",
    ),
    "PENDING_ORDER_AUTO_CANCEL_HOURS": (
        24,
        "How many hours an unpaid order can stay pending before it is "
        "automatically cancelled",
    ),
    # Task 44a (source doc 13.6). The master switch: OUT_OF_STOCK_BEHAVIOUR
    # below can be set to "backorder" independently, but that only takes
    # effect while this is also on -- an admin flipping the mode dropdown
    # alone can never silently start offering backorders. Off by default,
    # matching the doc's own stated current value.
    "BACKORDERS_ENABLED": (
        False,
        "Allow customers to order out-of-stock products",
    ),
    "BACKORDER_MESSAGE": (
        "This item is available for backorder and will ship soon.",
        "Message shown instead of 'Add to Cart' when a customer orders an "
        "out-of-stock item",
    ),
}

PRODUCT_AND_INVENTORY_SETTINGS = {
    # Task 41b (SPEC_PHASE2.md Feature 1, source doc 13.10 "Product Review
    # Approval"). Manual by default, matching the source doc's own stated
    # current value -- an admin can flip this to skip moderation entirely
    # once they trust the review volume/quality.
    "PRODUCT_REVIEW_AUTO_APPROVE_ENABLED": (
        False,
        "Auto-approve new product reviews instead of requiring manual "
        "admin approval",
    ),
    # Task 44a (source doc 13.10 "Out of Stock Behaviour"). "show" matches
    # the doc's own stated current value. "backorder" only actually takes
    # effect while BACKORDERS_ENABLED (Order Settings) is also on -- see
    # that setting's own comment.
    "OUT_OF_STOCK_BEHAVIOUR": (
        "show",
        "What happens to a product when its stock reaches zero",
        "out_of_stock_behaviour_field",
    ),
}

PROMOTIONS_SETTINGS = {
    # Task 43a (source doc 13.11 "Discount & Promotions Settings"). Source
    # doc's own stated current value is "On" -- gates the checkout entry
    # field itself (Task 43b), independent of any single code's own
    # is_active flag.
    "DISCOUNT_CODES_ENABLED": (
        True,
        "Global on/off for discount codes at checkout",
    ),
    # No numeric default given in the source doc (its own "Current Value"
    # column says only "Set by admin") -- confirmed directly with the user
    # 2026-08-13 as a generous starter cap (GHS 500), a safety net against
    # an accidental unlimited-discount code rather than a guess with no
    # basis. Always admin-editable afterward.
    "MAX_DISCOUNT_PER_ORDER": (
        Decimal("500"),
        "Maximum discount (GHS) a single order can receive, regardless of "
        "what any one code's own amount would otherwise apply",
        "non_negative_money_field",
    ),
    "DISCOUNT_APPLICABLE_TO": (
        "everyone",
        "Platform-wide default for who discount codes apply to -- "
        "distinct from each code's own audience restriction",
        "discount_audience_field",
    ),
}

REPORTING_SETTINGS = {
    # Task 46b (ADR-0010). "Once a day" cadence, matching
    # MATCHING_BONUS_INTERVAL_DAYS's own interval_days_field precedent
    # exactly rather than inventing minutes-based granularity for
    # something fundamentally daily -- a nightly rollup gives most of
    # yesterday's orders (PENDING_ORDER_AUTO_CANCEL_HOURS is 24h) time to
    # reach a stable status before this runs.
    "REPORT_ROLLUP_INTERVAL_DAYS": (
        1,
        "How often the background job recomputes daily sales/revenue " "report rollups",
        "interval_days_field",
    ),
}

COMPLIANCE_SETTINGS = {
    # Task 47a. Was a plain, unbounded 2-tuple (ESCROW_RESERVE_RATE) --
    # a doubt-driven-development review before implementation caught it
    # missing the percentage_field bound WITHHOLDING_TAX_RATE/
    # BINARY_BONUS_RATE/MATCHING_BONUS_RATE all already carry (an admin
    # could otherwise type 750 or a negative value with zero validation).
    # Also relocated here from WITHDRAWAL_AND_PAYOUT_SETTINGS -- this new
    # fieldset is where Task 47b's compliance-alert-email and Task 47e's
    # audit-log-retention settings will land too, not withdrawal-specific.
    "ESCROW_RESERVE_RATE": (
        Decimal("5"),
        # Honesty fix, same review: the seeded help text used to say
        # "held at GCB Bank" -- this is explicitly an internal-only
        # compliance ledger, never a real external bank integration
        # (user-confirmed), so the admin-facing text must say so.
        "Percentage of product revenue held internally as a notional "
        "compliance reserve (not a real external bank account)",
        "percentage_field",
    ),
    # Task 47b. 70% per the source doc (Section 13.12) -- the minimum
    # share of paid orders that must be genuine retail sales (Order.pv_earned
    # == 0), not distributor self-purchases, matching the real-world "70/30
    # rule" common to anti-pyramid-scheme MLM regulation.
    "RETAIL_PV_MINIMUM_PERCENT": (
        Decimal("70"),
        "Minimum percentage of paid orders that must be retail (non-"
        "distributor) sales before a compliance alert fires",
        "percentage_field",
    ),
    "COMPLIANCE_ALERT_EMAIL": (
        "",
        "Notified when the retail/distributor ratio drops below "
        "Retail PV Minimum (%)",
    ),
    # Task 47e, source doc 13.12 "Audit Log Retention Period (days)". No
    # source-doc-specified default was found -- 365 days (1 year) is a
    # reasonable, defensible starting point for a compliance-adjacent
    # record, admin-editable like every other seeded value in this file.
    # Governs apps.compliance.tasks.cleanup_expired_audit_records, a
    # daily Celery Beat job deleting HistoricalRecords() rows (Category/
    # Product/Distributor/Order/WithdrawalRequest) and PlatformSettingChange
    # rows older than this window.
    "AUDIT_LOG_RETENTION_PERIOD_DAYS": (
        365,
        "How many days of audit log history to retain before scheduled "
        "cleanup deletes it",
        "retention_days_field",
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
    # Task 36f: the old free-text "Social Media Links" decorative field that
    # used to live here (never read anywhere -- Task 35 built the real
    # SocialMediaLink model/CRUD instead) is removed entirely, not just
    # hidden -- it sat directly above the real Social Links section on this
    # same tab and read as a confusing duplicate of it.
    # Task 36e: was free text an admin had to type "GHS" into -- a bounded
    # choice field, matching this file's own established WITHDRAWAL_DAY/
    # WITHDRAWAL_FREQUENCY precedent (day_of_week_field/
    # withdrawal_frequency_field below), not a one-off custom widget.
    # Task 36h: locked to a single GHS choice, user-confirmed after being
    # asked directly -- every price display in this codebase currently
    # hardcodes a literal "GHS" prefix (grep confirms neither CURRENCY nor
    # CURRENCY_SYMBOL is read anywhere) and the Paystack merchant account
    # itself is GHS-only, so offering NGN/USD/GBP/EUR as if they were real
    # choices would silently do nothing while looking functional -- a real
    # footgun. Wiring every hardcoded price prefix (and the Paystack
    # currency parameter) to read a live multi-currency setting is real,
    # separate, unrequested scope, not something to fake with a dropdown.
    "CURRENCY": (
        "GHS",
        "Locked to GHS -- multi-currency support (Paystack, per-currency "
        "pricing) isn't built yet",
        "currency_field",
    ),
    "CURRENCY_SYMBOL": (
        "GHS",
        "Locked to GHS -- multi-currency support isn't built yet",
        "currency_field",
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
    "MAX_SOCIAL_MEDIA_LINKS": (
        20,
        "Maximum number of social media links an admin can add to the footer",
    ),
}

# Task 48d (source doc Section 13.8, SPEC_PHASE2.md Feature 8). SMS_PROVIDER/
# EMAIL_PROVIDER are locked single-choice fields, not a functional picker --
# mNotify (SMS) and Gmail SMTP (email) stay the only wired providers in code,
# matching the exact "Currency" precedent from Task 36h (currency_field
# below): a working-looking dropdown with nothing wired behind the other
# options is a real footgun, so it's honestly locked to the one real option
# instead. Real Arkesel/Hubtel/Mailgun integration is out of scope, per
# direct user confirmation during Phase 2 planning.
#
# SENDER_NAME/SENDER_EMAIL_ADDRESS default to "" (blank), not the current
# .env values -- blank means "use the server's configured default"
# (settings.MNOTIFY_SENDER_ID / settings.DEFAULT_FROM_EMAIL), read live by
# apps.notifications.sms._send_via_mnotify / apps.notifications.email
# .get_sender_email() respectively. This is deliberate, not an oversight:
# DEFAULT_FROM_EMAIL already has its own startup-time validation
# (bancostore/settings.py, Task 24d) rejecting known-insecure placeholder
# values -- moving the sender identity fully into a lazily-read runtime
# constance field would silently bypass that guarantee. Defaulting to blank
# keeps that validated env value as the live behavior until an admin
# deliberately opts into overriding it, rather than requiring this
# migration to duplicate Task 24d's own validation logic for a setting this
# task's own scope describes as "much smaller."
NOTIFICATION_AND_COMMUNICATION_SETTINGS = {
    "SMS_PROVIDER": (
        "mnotify",
        "The SMS provider used for OTP/withdrawal/order notifications",
        "sms_provider_field",
    ),
    "EMAIL_PROVIDER": (
        "gmail_smtp",
        "The email provider used for transactional email",
        "email_provider_field",
    ),
    "SENDER_NAME": (
        "",
        "The SMS sender ID shown to recipients -- leave blank to use the "
        "server's configured default",
    ),
    "SENDER_EMAIL_ADDRESS": (
        "",
        "The From address used for transactional email -- leave blank to "
        "use the server's configured default",
    ),
    # Task 63c. 200 user-confirmed 2026-09-17. Checked hourly by
    # apps.notifications.tasks.check_sms_credit. 0 turns the early warning
    # off; running out entirely is still announced.
    "SMS_LOW_CREDIT_THRESHOLD": (
        200,
        "Warn the admin when SMS credit falls below this many credits. Set to "
        "0 to be told only when credit runs out.",
        "non_negative_count_field",
    ),
    # Same blank-means-disabled convention as ADMIN_ORDER_ALERT_EMAIL. The
    # admin bell always rings regardless.
    "SMS_CREDIT_ALERT_EMAIL": (
        "",
        "Emailed when SMS credit is low or has run out. Leave blank to rely on "
        "the admin bell only.",
    ),
    # Task 67. Blank falls back to ADMIN_ORDER_ALERT_EMAIL rather than
    # meaning "off": this is money a customer has paid for nothing, and a
    # store that has already said where order mail goes should not also have
    # to find this setting before hearing about it. The bell always rings.
    "PAYMENT_ISSUE_ALERT_EMAIL": (
        "",
        "Emailed when a payment succeeds on Paystack but no account, starter "
        "pack or order came of it, so it can be refunded or fixed. Leave blank "
        "to use the admin order alert email.",
    ),
}

# Custom-bounded form fields for the small set of constance keys where an
# unbounded admin form submission is a real risk, not just a typo the admin
# would immediately notice -- added 2026-07-22 after a dedicated
# security-and-hardening review of the Binary Bonus batch driver found
# BINARY_BONUS_RATE/WEEKLY_BINARY_BONUS_CAP/BINARY_BONUS_INTERVAL_MINUTES
# had no server-side bounds, so a single fat-fingered admin form submission
# (e.g. 750 instead of 7.5) took effect immediately across the whole
# distributor base with no review step. Scoped to exactly those three keys
# -- other rate/cap settings in this file are a separate decision.
# Field/widget classes are string paths, not direct imports, per
# django-constance's own documented convention (avoids import-order issues
# since this module is imported very early, from bancostore/settings.py).
CONSTANCE_ADDITIONAL_FIELDS = {
    "percentage_field": [
        "django.forms.fields.DecimalField",
        {
            "widget": "django.forms.NumberInput",
            "min_value": 0,
            "max_value": 100,
            "decimal_places": 2,
        },
    ],
    "non_negative_money_field": [
        "django.forms.fields.DecimalField",
        {
            "widget": "django.forms.NumberInput",
            # No max: unlike a percentage, a GHS cap has no natural upper
            # bound this codebase is positioned to guess at. 0 is allowed
            # deliberately (see WEEKLY_BINARY_BONUS_CAP's fieldset entry) --
            # apply_weekly_binary_bonus_cap already treats a 0 cap as "no
            # room, pay nothing" with no crash, making it a legitimate
            # incident-response lever to pause binary bonus payouts without
            # touching the rate or disabling the whole job.
            "min_value": 0,
            "decimal_places": 2,
        },
    ],
    "interval_minutes_field": [
        "django.forms.fields.IntegerField",
        {
            "widget": "django.forms.NumberInput",
            # Matches the runtime floor _sync_periodic_task_interval already
            # enforces in apps/commissions/tasks.py -- this is defense in
            # depth (stop the bad value at the form) on top of that (stop
            # a bad value already in the DB, e.g. set via the ORM directly,
            # from ever reaching the live Celery Beat schedule).
            "min_value": 5,
        },
    ],
    "interval_days_field": [
        "django.forms.fields.IntegerField",
        {
            "widget": "django.forms.NumberInput",
            # Same reasoning as interval_minutes_field, scaled to a
            # days-cadence job (Matching Bonus) rather than a
            # minutes-cadence one (Binary Bonus) -- a floor of 1, not 5,
            # since "once a day" is still a perfectly sane matching-bonus
            # cadence, unlike "every 5 minutes" being the practical floor
            # for a job with real per-distributor DB work.
            "min_value": 1,
        },
    ],
    # Task 63c (CodeRabbit, PR #94). A plain count where negative has no
    # meaning -- SMS_LOW_CREDIT_THRESHOLD, where a negative value would
    # silently mean "never warn early".
    "non_negative_count_field": [
        "django.forms.fields.IntegerField",
        {
            "widget": "django.forms.NumberInput",
            "min_value": 0,
        },
    ],
    "non_negative_depth_field": [
        "django.forms.fields.IntegerField",
        {
            "widget": "django.forms.NumberInput",
            # A negative depth already degrades safely (sum_downline_
            # binary_bonus_earnings's depth >= max_depth check trips on
            # the very first iteration, returning 0 rather than paying
            # anything -- confirmed 2026-07-22 security-and-hardening
            # review), so this bound isn't closing a real exploit. Added
            # for consistency with this file's own stated policy of
            # bounding admin-editable numeric fields, and so a
            # fat-fingered negative value fails loudly on the form
            # instead of silently and confusingly zeroing out an admin's
            # intended depth.
            "min_value": 0,
        },
    ],
    "day_of_week_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # A bounded choice, not a free-text field -- WITHDRAWAL_DAY was a
            # plain string before this (SPEC.md Open Question #5, resolved
            # 2026-07-22), which would have let an admin type "friday" or
            # "Frday" with no validation at all.
            "choices": [
                ("monday", "Monday"),
                ("tuesday", "Tuesday"),
                ("wednesday", "Wednesday"),
                ("thursday", "Thursday"),
                ("friday", "Friday"),
                ("saturday", "Saturday"),
                ("sunday", "Sunday"),
            ],
        },
    ],
    "withdrawal_frequency_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 16c: only "weekly" is meaningful today --
            # apps/withdrawal/services.py::WITHDRAWAL_FREQUENCY_DURATIONS
            # has no other entry -- but a bounded single-choice field still
            # closes the "admin types something not in the mapping and
            # every withdrawal submission starts raising" failure mode a
            # free-text field left open. Extend both this list and that
            # mapping together if a second cadence is ever added.
            "choices": [("weekly", "Weekly")],
        },
    ],
    "currency_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 36e/36h. Started as a 5-currency list (GHS + the other
            # currencies most relevant to a Ghana-based storefront); the
            # user asked directly what changing it away from GHS would
            # actually do, and the honest answer was "nothing" -- no price
            # display or Paystack API call reads this setting, and the
            # Paystack merchant account itself is GHS-only, so the other 4
            # choices were purely decorative and looked like real,
            # functional options. Restricted to a single GHS entry, the
            # same "bounded single-choice field closes a failure mode"
            # reasoning already used by withdrawal_frequency_field above.
            # Extend this list only alongside real multi-currency support
            # (per-currency pricing, the Paystack currency parameter,
            # exchange-rate-safe commission math) -- not before.
            "choices": [("GHS", "Ghanaian Cedi (GHS)")],
        },
    ],
    "sms_provider_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 48d. Same "bounded single-choice field, honestly locked
            # to the one real option" reasoning as currency_field above --
            # mNotify is the only wired SMS provider (apps.notifications
            # .sms); extend this list only alongside a real second
            # integration.
            "choices": [("mnotify", "mNotify")],
        },
    ],
    "email_provider_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 48d. Gmail SMTP (via Django's own EMAIL_BACKEND config)
            # is the only wired email provider; extend this list only
            # alongside a real second integration (e.g. Mailgun).
            "choices": [("gmail_smtp", "Gmail SMTP")],
        },
    ],
    "discount_audience_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 43a, source doc 13.11 "Discount Applicable To": "Retail
            # customers only, distributors only, or both" -- a bounded
            # choice, matching this file's own established reasoning for
            # every audience-style setting (day_of_week_field,
            # currency_field) rather than a free-text field.
            "choices": [
                ("everyone", "Everyone"),
                ("retail", "Retail Customers Only"),
                ("distributor", "Distributors Only"),
            ],
        },
    ],
    "out_of_stock_behaviour_field": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            # Task 44a, source doc 13.10 "Out of Stock Behaviour": "Hide
            # product, show 'Out of Stock', or allow backorders" -- a
            # bounded choice, matching this file's own established
            # reasoning for every mode-style setting.
            "choices": [
                ("hide", "Hide Product"),
                ("show", "Show 'Out of Stock'"),
                ("backorder", "Allow Backorders"),
            ],
        },
    ],
    "retention_days_field": [
        "django.forms.fields.IntegerField",
        {
            "widget": "django.forms.NumberInput",
            # Task 47e. This project's other "how many days" settings
            # (PV_CARRY_FORWARD_EXPIRY_DAYS, PV_EXPIRY_WARNING_DAYS) are
            # plain unbounded IntegerFields -- fine there, since they only
            # govern a business CALCULATION. This one governs actual
            # DELETION of audit-log rows (apps.compliance.tasks
            # .cleanup_expired_audit_records): a 0 or negative value would
            # be a real, un-undoable data-loss footgun, not just a
            # miscalculation. 30 days is a floor generous enough that no
            # plausible legitimate retention policy would ever need to go
            # below it, closing the fat-fingered-zero failure mode at its
            # source rather than only in the cleanup task's own code.
            "min_value": 30,
        },
    ],
}

CONSTANCE_CONFIG = {
    **AUTHENTICATION_SETTINGS,
    **COMMISSION_AND_BONUS_SETTINGS,
    **REGISTRATION_AND_MEMBERSHIP_SETTINGS,
    **WITHDRAWAL_AND_PAYOUT_SETTINGS,
    **DELIVERY_SETTINGS,
    **ORDER_SETTINGS,
    **PRODUCT_AND_INVENTORY_SETTINGS,
    **PROMOTIONS_SETTINGS,
    **REPORTING_SETTINGS,
    **COMPLIANCE_SETTINGS,
    **KYC_SETTINGS,
    **IR_ID_NUMBER_SETTINGS,
    **PAYMENT_GATEWAY_SETTINGS,
    **GENERAL_PLATFORM_SETTINGS,
    **NOTIFICATION_AND_COMMUNICATION_SETTINGS,
}

CONSTANCE_CONFIG_FIELDSETS = {
    "Authentication Settings": tuple(AUTHENTICATION_SETTINGS),
    "Commission & Bonus Settings": tuple(COMMISSION_AND_BONUS_SETTINGS),
    "Registration & Membership Settings": tuple(REGISTRATION_AND_MEMBERSHIP_SETTINGS),
    "Withdrawal & Payout Settings": tuple(WITHDRAWAL_AND_PAYOUT_SETTINGS),
    "Delivery Settings": tuple(DELIVERY_SETTINGS),
    "Order Settings": tuple(ORDER_SETTINGS),
    "Product & Inventory Settings": tuple(PRODUCT_AND_INVENTORY_SETTINGS),
    "Promotions Settings": tuple(PROMOTIONS_SETTINGS),
    "Reporting Settings": tuple(REPORTING_SETTINGS),
    "Compliance Settings": tuple(COMPLIANCE_SETTINGS),
    "KYC Settings": tuple(KYC_SETTINGS),
    "IR ID Number Settings": tuple(IR_ID_NUMBER_SETTINGS),
    "Payment Gateway Settings": tuple(PAYMENT_GATEWAY_SETTINGS),
    "General Platform Settings": tuple(GENERAL_PLATFORM_SETTINGS),
    "Notification & Communication Settings": tuple(
        NOTIFICATION_AND_COMMUNICATION_SETTINGS
    ),
}

# A plain str.title() on an underscore-joined identifier reads fine for
# ordinary words ("Distributor Login Method") but mangles the acronyms
# scattered through this codebase's own setting/field names ("Otp", "Kyc",
# "Pv", "Ir Id" instead of "OTP", "KYC", "PV", "IR ID") -- corrected here
# rather than leaving the raw identifier on screen, which is what actually
# prompted this (a bare "DISTRIBUTOR_LOGIN_METHOD" reads as code, not a
# setting a non-technical admin can recognize). Originally built for Task
# 28's Platform Settings screen (constance keys are UPPER_SNAKE_CASE);
# reused as-is by Task 47e/48's Audit Log screen for model field names
# (lower_snake_case) -- str.title() is case-insensitive to the input, so
# the same word-override table applies to both without change.
_LABEL_WORD_OVERRIDES = {
    "Otp": "OTP",
    "Kyc": "KYC",
    "Pv": "PV",
    "Ir": "IR",
    "Id": "ID",
    "Sms": "SMS",
    "Whatsapp": "WhatsApp",
    # str.title() capitalizes only the letter right after the digit --
    # "ADMIN_2FA_ENABLED".title() comes out "Admin 2Fa Enabled", not "2FA".
    "2Fa": "2FA",
}


def humanize_identifier_name(name):
    words = name.replace("_", " ").title().split(" ")
    return " ".join(_LABEL_WORD_OVERRIDES.get(word, word) for word in words)
