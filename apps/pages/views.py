import logging
import re

from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage
from django.shortcuts import redirect, render

from constance import config
from django_ratelimit.decorators import ratelimit

from apps.notifications.email import get_sender_email

from .forms import ContactForm
from .hcaptcha import hcaptcha_enabled, verify_hcaptcha

logger = logging.getLogger(__name__)


def about(request):
    return render(request, "pages/about.html")


def terms_of_use(request):
    return render(
        request,
        "pages/legal/terms-of-use.html",
        {
            "terms_and_conditions_text": _stripped_or_none(
                config.TERMS_AND_CONDITIONS_TEXT
            ),
            # CodeRabbit finding: this figure was hardcoded as plain text
            # ("currently 7 days") instead of reading the live setting --
            # the exact "decorative claim that can silently go stale the
            # moment an admin changes the real setting" bug class this
            # project has fixed before (see cooling_off_period_days on the
            # Returns/Refunds/Shipping page for the established pattern).
            "cooling_off_period_days": config.COOLING_OFF_PERIOD_DAYS,
        },
    )


def privacy_policy(request):
    return render(
        request,
        "pages/legal/privacy-policy.html",
        {"privacy_policy_text": _stripped_or_none(config.PRIVACY_POLICY_TEXT)},
    )


def cookie_policy(request):
    return render(request, "pages/legal/cookie-policy.html")


def disclaimer(request):
    return render(request, "pages/legal/disclaimer.html")


def earnings_disclosure(request):
    return render(request, "pages/legal/earnings-disclosure.html")


def ai_disclaimer(request):
    return render(request, "pages/legal/ai-disclaimer.html")


def returns_refunds_shipping(request):
    return render(
        request,
        "pages/legal/returns-refunds-shipping.html",
        {
            "delivery_fee_kumasi": config.DELIVERY_FEE_KUMASI,
            "delivery_fee_accra": config.DELIVERY_FEE_ACCRA,
            "delivery_fee_other_regions": config.DELIVERY_FEE_OTHER_REGIONS,
            "free_delivery_threshold": config.FREE_DELIVERY_THRESHOLD,
            "pending_order_auto_cancel_hours": config.PENDING_ORDER_AUTO_CANCEL_HOURS,
            "cooling_off_period_days": config.COOLING_OFF_PERIOD_DAYS,
            "refund_return_policy_text": _stripped_or_none(
                config.REFUND_RETURN_POLICY_TEXT
            ),
        },
    )


def getting_started_guide(request):
    return render(
        request,
        "pages/legal/getting-started-guide.html",
        {
            "registration_fee": config.REGISTRATION_FEE,
            "starter_pack_a_price": config.STARTER_PACK_A_PRICE,
            "starter_pack_a_pv": config.STARTER_PACK_A_PV,
            "starter_pack_a_rank": config.STARTER_PACK_A_RANK,
            "starter_pack_b_price": config.STARTER_PACK_B_PRICE,
            "starter_pack_b_pv": config.STARTER_PACK_B_PV,
            "starter_pack_b_rank": config.STARTER_PACK_B_RANK,
            "direct_referral_bonus_rate": config.DIRECT_REFERRAL_BONUS_RATE,
            "binary_bonus_rate": config.BINARY_BONUS_RATE,
            "matching_bonus_rate": config.MATCHING_BONUS_RATE,
            "matching_bonus_depth_bronze": config.MATCHING_BONUS_DEPTH_BRONZE,
            "matching_bonus_depth_silver": config.MATCHING_BONUS_DEPTH_SILVER,
            "min_monthly_personal_pv": config.MIN_MONTHLY_PERSONAL_PV,
            "pv_carry_forward_expiry_days": config.PV_CARRY_FORWARD_EXPIRY_DAYS,
            "min_withdrawal_amount": config.MIN_WITHDRAWAL_AMOUNT,
            "max_withdrawal_amount": config.MAX_WITHDRAWAL_AMOUNT,
            "withdrawal_frequency": config.WITHDRAWAL_FREQUENCY,
            "withdrawal_day": config.WITHDRAWAL_DAY,
            "withholding_tax_rate": config.WITHHOLDING_TAX_RATE,
        },
    )


def _stripped_or_none(value):
    """A constance value defaults to "" and is meant to stay unset until
    an admin fills it in via the Platform Settings screen -- a
    whitespace-only value must not be treated as "the admin set this"
    (doubt-driven-development finding)."""
    value = (value or "").strip()
    return value or None


def _whatsapp_link(number):
    # Code-review finding: an admin can enter a phone number in any
    # common local format (dashes, spaces, parentheses) -- wa.me only
    # accepts digits, so anything else must be stripped rather than
    # just "+" and " " (which left a broken link for every other format).
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    return f"https://wa.me/{digits}" if digits else None


@ratelimit(key="ip", rate="5/h", method="POST")
def contact(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        form_is_valid = form.is_valid()
        if form.is_bot_trap_filled:
            # Honeypot triggered -- respond exactly like a real success so
            # an automated submitter gets no signal it was caught, but
            # never actually send anything. Checked independent of
            # form_is_valid (CodeRabbit finding, PR #81): honeypot is
            # required=False, so it lands in cleaned_data via
            # full_clean()'s per-field cleaning regardless of whether
            # some other field also failed -- a bot that fills the
            # honeypot but leaves a required field blank must still hit
            # this path, not fall through to real validation errors.
            logger.info("contact: honeypot field was filled, discarding submission")
            messages.success(
                request,
                "Thank you — your message has been sent. We'll get back "
                "to you soon.",
            )
            return redirect("pages:contact")

        captcha_failed = (
            form_is_valid
            and hcaptcha_enabled()
            and not verify_hcaptcha(request.POST.get("h-captcha-response", ""))
        )
        if captcha_failed:
            # No field on the form maps to the CAPTCHA widget (it's not a
            # Django form field -- see templates/pages/contact.html), so
            # this surfaces the same way every other non-field failure on
            # this view does: a flash message on re-render, matching the
            # send-failure path below.
            messages.error(request, "CAPTCHA verification failed. Please try again.")
        elif form_is_valid:
            configured_recipient = _stripped_or_none(config.CONTACT_EMAIL_ADDRESS)
            if configured_recipient:
                recipient = configured_recipient
            else:
                # Code-review finding: without this, a submission before
                # an admin sets CONTACT_EMAIL_ADDRESS silently lands in
                # the "no-reply" inbox nobody reads -- a black hole with
                # no signal anywhere that the contact form is
                # unconfigured.
                logger.warning(
                    "contact: CONTACT_EMAIL_ADDRESS is not configured -- "
                    "falling back to DEFAULT_FROM_EMAIL"
                )
                recipient = settings.DEFAULT_FROM_EMAIL
            body = (
                f"{form.cleaned_data['message']}\n\n"
                f"---\n"
                f"From: {form.cleaned_data['name']} <{form.cleaned_data['email']}>"
            )
            try:
                EmailMessage(
                    subject=f"[Bancostore Contact] {form.cleaned_data['subject']}",
                    body=body,
                    from_email=get_sender_email(),
                    to=[recipient],
                    reply_to=[form.cleaned_data["email"]],
                ).send()
            except Exception:
                logger.exception(
                    "contact: failed to send message from %s",
                    form.cleaned_data["email"],
                )
                messages.error(
                    request,
                    "Sorry, we couldn't send your message right now. Please "
                    "try again shortly.",
                )
            else:
                messages.success(
                    request,
                    "Thank you — your message has been sent. We'll get back "
                    "to you soon.",
                )
                return redirect("pages:contact")
    else:
        form = ContactForm()

    whatsapp_support_number = _stripped_or_none(config.WHATSAPP_SUPPORT_NUMBER)
    return render(
        request,
        "pages/contact.html",
        {
            "form": form,
            "contact_phone_number": _stripped_or_none(config.CONTACT_PHONE_NUMBER),
            "contact_email_address": _stripped_or_none(config.CONTACT_EMAIL_ADDRESS),
            "whatsapp_support_number": whatsapp_support_number,
            "whatsapp_link": _whatsapp_link(whatsapp_support_number),
            "physical_address": _stripped_or_none(config.PHYSICAL_ADDRESS),
            "hcaptcha_site_key": (
                settings.HCAPTCHA_SITE_KEY if hcaptcha_enabled() else None
            ),
        },
    )
