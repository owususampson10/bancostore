import hashlib
import hmac

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


def verify_webhook_signature(session_id, status, created_at, signature_header) -> bool:
    """UNVERIFIED against Didit's own primary docs -- docs.didit.me's webhook
    page 404'd via direct fetch during planning; this scheme (X-Signature-
    Simple = HMAC-SHA256 of "{session_id}|{status}|{created_at}" using
    DIDIT_WEBHOOK_SECRET) is sourced from a community demo repo's README
    description of Didit's "Simple Signature" method (the recommended of
    Didit's three supported schemes -- V2/Original aren't implemented here,
    matching Rule 0's "don't generalize until needed"), not confirmed
    first-party. MUST be re-verified against a real webhook delivery once
    Task 11b is tested end-to-end with the user's actual Didit workflow --
    see tasks/todo.md Task 11b. Constant-time comparison, same reasoning as
    Paystack's verify_webhook_signature."""
    if not signature_header:
        return False
    message = f"{session_id}|{status}|{created_at}"
    computed = hmac.new(
        settings.DIDIT_WEBHOOK_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature_header)
