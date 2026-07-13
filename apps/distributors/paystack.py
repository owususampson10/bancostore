import hashlib
import hmac

from django.conf import settings

import requests

PAYSTACK_BASE_URL = "https://api.paystack.co"
REQUEST_TIMEOUT_SECONDS = 10


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
