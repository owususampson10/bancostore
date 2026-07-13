import hashlib
import hmac
from unittest.mock import Mock, patch

from django.test import override_settings

import pytest
import requests

from apps.distributors.didit import (
    DiditError,
    create_verification_session,
    get_session_decision,
    verify_webhook_signature,
)


def _fake_response(json_data, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error"
        )
    else:
        response.raise_for_status.return_value = None
    return response


@override_settings(DIDIT_API_KEY="fake-api-key", DIDIT_WORKFLOW_ID="workflow-123")
@patch("apps.distributors.didit.requests.post")
def test_create_verification_session_returns_the_parsed_response(mock_post):
    mock_post.return_value = _fake_response(
        {
            "session_id": "sess-abc",
            "session_number": 1,
            "vendor_data": "42",
            "status": "Not Started",
            "workflow_id": "workflow-123",
            "callback": "https://bancostore.test/kyc/callback/",
            "url": "https://verify.didit.me/session/sess-abc",
        }
    )

    result = create_verification_session(
        callback_url="https://bancostore.test/kyc/callback/", vendor_data=42
    )

    assert result["session_id"] == "sess-abc"
    assert result["url"] == "https://verify.didit.me/session/sess-abc"

    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["x-api-key"] == "fake-api-key"
    assert call_kwargs["json"]["workflow_id"] == "workflow-123"
    assert call_kwargs["json"]["callback"] == "https://bancostore.test/kyc/callback/"
    assert call_kwargs["json"]["vendor_data"] == "42"
    assert call_kwargs["timeout"] is not None


@override_settings(DIDIT_API_KEY="fake-api-key", DIDIT_WORKFLOW_ID="workflow-123")
@patch("apps.distributors.didit.requests.post")
def test_create_verification_session_raises_didit_error_on_http_failure(mock_post):
    mock_post.return_value = _fake_response({"detail": "forbidden"}, status_code=403)

    with pytest.raises(DiditError):
        create_verification_session(
            callback_url="https://bancostore.test/kyc/callback/", vendor_data=42
        )


@override_settings(DIDIT_API_KEY="fake-api-key", DIDIT_WORKFLOW_ID="workflow-123")
@patch("apps.distributors.didit.requests.post")
def test_create_verification_session_raises_didit_error_on_network_failure(mock_post):
    mock_post.side_effect = requests.ConnectionError("network down")

    with pytest.raises(DiditError):
        create_verification_session(
            callback_url="https://bancostore.test/kyc/callback/", vendor_data=42
        )


@override_settings(DIDIT_API_KEY="fake-api-key")
@patch("apps.distributors.didit.requests.get")
def test_get_session_decision_returns_the_parsed_response(mock_get):
    mock_get.return_value = _fake_response(
        {
            "session_id": "sess-abc",
            "status": "Approved",
            "id_verifications": [{"status": "Approved", "first_name": "Ama"}],
            "face_matches": [{"status": "Approved", "score": 0.97}],
            "liveness_checks": [{"status": "Approved", "score": 0.99}],
            "warnings": [],
        }
    )

    result = get_session_decision("sess-abc")

    assert result["status"] == "Approved"
    assert result["face_matches"][0]["score"] == 0.97
    call_args, call_kwargs = mock_get.call_args
    assert "sess-abc" in call_args[0]
    assert call_kwargs["headers"]["x-api-key"] == "fake-api-key"
    assert call_kwargs["timeout"] is not None


@override_settings(DIDIT_API_KEY="fake-api-key")
@patch("apps.distributors.didit.requests.get")
def test_get_session_decision_raises_didit_error_on_http_failure(mock_get):
    mock_get.return_value = _fake_response({"detail": "not found"}, status_code=404)

    with pytest.raises(DiditError):
        get_session_decision("sess-abc")


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
def test_verify_webhook_signature_accepts_a_correctly_computed_signature():
    session_id, status, created_at = "sess-abc", "Approved", "2026-07-13T10:00:00Z"
    message = f"{session_id}|{status}|{created_at}".encode("utf-8")
    correct_signature = hmac.new(b"whsec_fake", message, hashlib.sha256).hexdigest()

    assert (
        verify_webhook_signature(session_id, status, created_at, correct_signature)
        is True
    )


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
def test_verify_webhook_signature_rejects_a_wrong_signature():
    assert (
        verify_webhook_signature(
            "sess-abc", "Approved", "2026-07-13T10:00:00Z", "not-the-right-signature"
        )
        is False
    )


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
def test_verify_webhook_signature_rejects_a_missing_header():
    assert (
        verify_webhook_signature("sess-abc", "Approved", "2026-07-13T10:00:00Z", "")
        is False
    )
    assert (
        verify_webhook_signature("sess-abc", "Approved", "2026-07-13T10:00:00Z", None)
        is False
    )


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
def test_verify_webhook_signature_rejects_a_tampered_status():
    """The signature is computed over session_id|status|created_at -- if any
    of those three values is altered after the signature was issued,
    verification must fail."""
    session_id, created_at = "sess-abc", "2026-07-13T10:00:00Z"
    message = f"{session_id}|Approved|{created_at}".encode("utf-8")
    signature = hmac.new(b"whsec_fake", message, hashlib.sha256).hexdigest()

    assert (
        verify_webhook_signature(session_id, "Declined", created_at, signature) is False
    )
