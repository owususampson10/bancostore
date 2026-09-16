import hashlib
import hmac
import re

from django.conf import settings

import requests

from .models import Distributor

PAYSTACK_BASE_URL = "https://api.paystack.co"
REQUEST_TIMEOUT_SECONDS = 10

# Task 16e (code-review-and-quality finding): ADR-0004 frames mapping each
# MobileMoneyNetwork to its Paystack bank_code as this task's own job, not
# just something to document -- these are real, static institution codes
# confirmed via a live call to list_banks() against Paystack's test-mode
# API (2026-07-23), not guessed from a web search. A future caller (Task
# 16f) should use this dict directly rather than hand-transcribing values
# out of a comment, or calling list_banks() again at runtime for values
# that don't change. Paystack's own bank list still labels the Telecel
# network "Vodafone" despite the real-world Vodafone-to-Telecel rebrand
# ADR-0004 already flagged -- VOD is still the correct code to send.
MOBILE_MONEY_BANK_CODES = {
    Distributor.MobileMoneyNetwork.MTN: "MTN",
    Distributor.MobileMoneyNetwork.TELECEL: "VOD",
    Distributor.MobileMoneyNetwork.AIRTELTIGO: "ATL",
}


class PaystackError(Exception):
    pass


class PaystackNotFoundError(PaystackError):
    """Raised specifically when Paystack returns 404 -- e.g.
    verify_transfer() called against a reference Paystack has never
    seen (Task 16f: distinguishing this from every other failure is
    what lets a payout retry safely tell "not yet initiated" apart from
    "initiated, but something else went wrong verifying it"). A subclass
    of PaystackError, not a sibling -- every existing caller that only
    catches the base class keeps working unchanged."""


# Task 59: the domain a placeholder payer address is built on.
#
# A subdomain, not bancostore.com itself, and deliberately one with no MX
# record: bancostore.com has real mail servers (secureserver.net, checked
# 2026-09-15), so a placeholder address there would genuinely deliver
# Paystack's receipt into a live mailbox or a catch-all. A lookup for
# guests.bancostore.com finds nothing, so the receipt is rejected at the
# sending server and never reaches anyone.
#
# Paystack's API only validates the address's SHAPE, never its
# deliverability -- which is what makes this work, and is also why the
# original "@bancostore.test" failed: ".test" is a reserved TLD (RFC
# 2606), and combined with the "+" carried over from the E.164 phone
# number, live Paystack rejected the whole payload with 400.
PLACEHOLDER_EMAIL_DOMAIN = "guests.bancostore.com"


def paystack_customer_email(contact_email, phone_number):
    """Paystack requires an email on every transaction, but a customer's
    email is optional everywhere in this codebase (SPEC.md Section 4
    lists it as "for notifications only") -- a guest checking out, a
    distributor registering by phone, and a distributor buying a starter
    pack can all legitimately have none.

    Task 59, confirmed against the live API in a real browser: the
    previous fallback built "+233241234567@bancostore.test" from the
    E.164 phone number, and Paystack's LIVE mode rejects that payload
    with 400 Bad Request. Test mode accepted it, which is why all three
    payment entry points shipped broken for email-less users and stayed
    that way until the account went live. Either the "+" in the local
    part or the reserved ".test" TLD could have been the trigger; this
    avoids both rather than guessing which one mattered.

    The fallback stays distinct per phone number on purpose -- one
    shared address for every guest would collapse them into a single
    customer on Paystack's own dashboard. It is never persisted onto
    Order.email or User.email; it exists only for the duration of the
    API call.
    """
    contact_email = (contact_email or "").strip()
    if contact_email:
        return contact_email
    digits = re.sub(r"\D", "", str(phone_number or ""))
    local_part = f"guest-{digits}" if digits else "guest"
    return f"{local_part}@{PLACEHOLDER_EMAIL_DOMAIN}"


def _auth_headers():
    return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}


# Task 59: bounded because not every failure body is Paystack's own small
# JSON -- a proxy or gateway in front of the API can return a full HTML
# error page, and every caller below funnels this straight into a
# logger.exception() call on a production server.
MAX_LOGGED_RESPONSE_BODY_CHARS = 500


def _response_body_detail(response):
    """Task 59: requests.HTTPError's own string form is only ever
    "401 Client Error: Unauthorized for url: ..." -- it carries the
    status and nothing else. Paystack's actual explanation for the
    failure ("Invalid key", "Business not activated", a rejected
    payload field) lives in the response body, which was being dropped
    entirely. A real live-key checkout failure on production could not
    be diagnosed from the log because of exactly that gap.

    Safe to log: Paystack error bodies describe the request's outcome,
    never echoing back the Authorization header the request was sent
    with, so no secret material passes through here.

    Returns a fragment ready to append to an error message, or "" when
    there is no body worth reporting (a timeout has no response at all).
    """
    if response is None:
        return ""
    body = (response.text or "").strip()
    if not body:
        return ""
    if len(body) > MAX_LOGGED_RESPONSE_BODY_CHARS:
        body = body[:MAX_LOGGED_RESPONSE_BODY_CHARS] + "... (truncated)"
    return f" -- response body: {body}"


def _raise_as_paystack_error(exc, action):
    """Shared by every wrapper function's except block below. Raises
    PaystackNotFoundError for a 404 specifically, the base PaystackError
    for everything else (including network-level failures with no
    response at all, e.g. a timeout -- those are "we don't know", not
    "not found", and must not be treated as the same thing)."""
    response = getattr(exc, "response", None)
    detail = _response_body_detail(response)
    if response is not None and response.status_code == 404:
        raise PaystackNotFoundError(f"Paystack {action} failed: {exc}{detail}") from exc
    raise PaystackError(f"Paystack {action} failed: {exc}{detail}") from exc


def initialize_transaction(*, email, amount_pesewas, reference, callback_url):
    """Source: https://paystack.com/docs/api/transaction/ ("Initialize
    Transaction"). POST /transaction/initialize; amount is in the subunit
    of the currency (pesewas for GHS). Returns the response's `data` object
    (authorization_url, access_code, reference)."""
    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            headers=_auth_headers(),
            json={
                "email": email,
                "amount": str(amount_pesewas),
                "reference": reference,
                "callback_url": callback_url,
                "currency": "GHS",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "initialize_transaction")
    return response.json()["data"]


def verify_transaction(reference):
    """Source: https://paystack.com/docs/api/transaction/ ("Verify
    Transaction"). GET /transaction/verify/:reference -- the authoritative
    source of a transaction's status/amount/currency; never trust a
    webhook body's own claims about these instead of calling this."""
    try:
        response = requests.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "verify_transaction")
    return response.json()["data"]


def list_banks(*, country="ghana", currency="GHS", bank_type="mobile_money"):
    """Source: https://github.com/PaystackOSS/openapi
    (paths/bank.yaml, "List Banks" -- paystack.com/docs itself returns a
    403 to automated fetches, so this is grounded in Paystack's own
    published OpenAPI spec instead). GET /bank?country=&currency=&type=.

    This is how MOBILE_MONEY_BANK_CODES above was originally confirmed;
    callers creating a transfer recipient should use that dict directly
    rather than calling this at runtime for values that don't change."""
    try:
        response = requests.get(
            f"{PAYSTACK_BASE_URL}/bank",
            headers=_auth_headers(),
            params={"country": country, "currency": currency, "type": bank_type},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "list_banks")
    return response.json()["data"]


def create_transfer_recipient(*, name, account_number, bank_code, currency="GHS"):
    """Source: https://github.com/PaystackOSS/openapi
    (paths/transferrecipient.yaml, schemas/TransferRecipientCreate.yaml).
    POST /transferrecipient. type="mobile_money" is hardcoded -- Ghana
    mobile money is the only payout method this project supports
    (ADR-0004: bank-account payout is a recorded Phase 2 item, not built
    here). A duplicate account_number returns Paystack's own existing
    recipient record rather than erroring (documented Paystack behavior,
    not something this wrapper needs to guard against). Returns the
    response's `data` object (recipient_code, details, etc.)."""
    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/transferrecipient",
            headers=_auth_headers(),
            json={
                "type": "mobile_money",
                "name": name,
                "account_number": account_number,
                "bank_code": bank_code,
                "currency": currency,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "create_transfer_recipient")
    return response.json()["data"]


def initiate_transfer(*, amount_pesewas, recipient_code, reference, reason=""):
    """Source: https://github.com/PaystackOSS/openapi
    (paths/transfer.yaml, schemas/TransferBase.yaml,
    schemas/TransferInitiate.yaml). POST /transfer. amount is in the
    subunit of the currency (pesewas for GHS, same convention as
    initialize_transaction). `reference` must be a unique idempotency
    key the caller generates -- Paystack's own docs require a lowercase
    alphanumeric string with only -,_ symbols, at least 16 characters;
    generating one that meets that shape is the caller's job (Task 16f),
    not this wrapper's. source="balance" is hardcoded -- pulling from
    the Paystack account balance is the only transfer source this
    platform uses."""
    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/transfer",
            headers=_auth_headers(),
            json={
                "source": "balance",
                "amount": str(amount_pesewas),
                "recipient": recipient_code,
                "reference": reference,
                "reason": reason,
                "currency": "GHS",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "initiate_transfer")
    return response.json()["data"]


def verify_transfer(reference):
    """Source: https://github.com/PaystackOSS/openapi
    (paths/transfer_verify_{reference}.yaml,
    schemas/TransferVerifyResponse.yaml). GET /transfer/verify/:reference
    -- the authoritative source of a transfer's status; never trust a
    webhook body's own claims about this instead of calling this (same
    rule verify_transaction and the Didit integration already
    established)."""
    try:
        response = requests.get(
            f"{PAYSTACK_BASE_URL}/transfer/verify/{reference}",
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_as_paystack_error(exc, "verify_transfer")
    return response.json()["data"]


def verify_webhook_signature(raw_body: bytes, signature_header) -> bool:
    """Source: https://paystack.com/docs/payments/webhooks/ ("Verify event
    origin"). x-paystack-signature is HMAC-SHA512 of the raw request body,
    signed with the secret key. Must hash raw_body directly -- re-encoding
    parsed JSON can produce different bytes (whitespace/key order) and
    silently break verification. Constant-time comparison to avoid a
    timing side-channel on the signature check itself."""
    if not signature_header:
        return False
    computed = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed, signature_header)
