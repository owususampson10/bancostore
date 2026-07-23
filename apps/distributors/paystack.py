import hashlib
import hmac

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


def _auth_headers():
    return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}


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
        raise PaystackError(f"Paystack initialize_transaction failed: {exc}") from exc
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
        raise PaystackError(f"Paystack verify_transaction failed: {exc}") from exc
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
        raise PaystackError(f"Paystack list_banks failed: {exc}") from exc
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
        raise PaystackError(
            f"Paystack create_transfer_recipient failed: {exc}"
        ) from exc
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
        raise PaystackError(f"Paystack initiate_transfer failed: {exc}") from exc
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
        raise PaystackError(f"Paystack verify_transfer failed: {exc}") from exc
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
