import hashlib
import hmac
import json
import time

from django.conf import settings

import requests

DIDIT_BASE_URL = "https://verification.didit.me"
REQUEST_TIMEOUT_SECONDS = 10


class DiditError(Exception):
    pass


def _auth_headers():
    return {"x-api-key": settings.DIDIT_API_KEY}


def create_verification_session(*, callback_url, vendor_data):
    """Source: https://docs.didit.me/reference/create-session-verification-sessions
    ("Create Session"). POST /v3/session/ with the workflow configured in
    Didit's dashboard (bundles ID Verification + Face Match + Liveness --
    see DIDIT_WORKFLOW_ID). `callback` is where Didit redirects the
    distributor back to after they finish (or abandon) the hosted flow --
    same role as Paystack's callback_url. `vendor_data` is our own
    identifier (the distributor's pk) so we know who a later webhook/
    callback belongs to. Returns the response body (session_id, url, ...)."""
    try:
        response = requests.post(
            f"{DIDIT_BASE_URL}/v3/session/",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "workflow_id": settings.DIDIT_WORKFLOW_ID,
                "callback": callback_url,
                "vendor_data": str(vendor_data),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DiditError(f"Didit create_verification_session failed: {exc}") from exc
    return response.json()


def get_session_decision(session_id):
    """Source: https://docs.didit.me/sessions-api/retrieve-session
    ("Retrieve Session" decision). GET /v3/session/{session_id}/decision/ --
    the authoritative result (status, id_verifications, face_matches,
    liveness_checks, warnings, ...). Never trust a callback query string or
    webhook payload's own claims about the result instead of calling this,
    same rule already applied to Paystack's verify_transaction."""
    try:
        response = requests.get(
            f"{DIDIT_BASE_URL}/v3/session/{session_id}/decision/",
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DiditError(f"Didit get_session_decision failed: {exc}") from exc
    return response.json()


def _shorten_floats(data):
    """Didit's canonical form serializes whole-valued floats as ints (e.g.
    1.0 -> 1) before hashing -- without this, a value that round-trips
    through JSON as 1.0 instead of 1 would produce a different signature
    than Didit computed on their side."""
    if isinstance(data, dict):
        return {key: _shorten_floats(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_shorten_floats(item) for item in data]
    if isinstance(data, float) and data.is_integer():
        return int(data)
    return data


def verify_webhook_signature(payload: dict, timestamp: str, signature_header) -> bool:
    """Source: https://docs.didit.me/integration/webhooks ("X-Signature-V2").
    Confirmed 2026-07-14 against Didit's own primary docs (a second, more
    careful research pass -- the page had previously 404'd; this project's
    earlier implementation used the weaker "Simple" scheme sourced from a
    secondary community reference, which Didit's own docs explicitly say
    "does NOT authenticate decision data").

    V2 signs the canonical form of the *entire webhook payload*: whole-
    valued floats normalized to ints, then JSON-serialized with sorted
    keys, no extra whitespace, and Unicode preserved (not escaped) --
    `json.dumps(_shorten_floats(payload), sort_keys=True,
    separators=(",", ":"), ensure_ascii=False)`. HMAC-SHA256 over that
    string, using DIDIT_WEBHOOK_SECRET. Also enforces a timestamp
    freshness window (reject if the X-Timestamp header is more than 5
    minutes from now) to prevent replaying an old, validly-signed webhook.
    """
    if not signature_header or not timestamp:
        return False
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    canonical = json.dumps(
        _shorten_floats(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    computed = hmac.new(
        settings.DIDIT_WEBHOOK_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature_header)
