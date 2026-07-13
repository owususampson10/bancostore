import hashlib
import hmac
import json
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

import pytest

from apps.distributors.didit import DiditError
from apps.distributors.models import DiditVerification, Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(phone_verified=True):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, phone_verified=phone_verified
    )


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_start_kyc_verification_requires_login(client):
    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_phone_unverified_distributor_is_blocked(client):
    distributor = _make_distributor(phone_verified=False)
    _login(client, distributor)

    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 200
    assert "Verify your phone number" in response.content.decode()


@pytest.mark.django_db
def test_already_approved_distributor_is_redirected_to_dashboard(client):
    distributor = _make_distributor()
    distributor.kyc_status = Distributor.KycStatus.APPROVED
    distributor.save(update_fields=["kyc_status"])
    _login(client, distributor)

    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:dashboard")


@pytest.mark.django_db
@patch("apps.distributors.views.create_didit_session")
def test_post_creates_a_session_and_redirects(mock_create_session, client):
    distributor = _make_distributor()
    _login(client, distributor)
    mock_create_session.return_value = {
        "session_id": "sess-abc",
        "url": "https://verify.didit.me/session/sess-abc",
    }

    response = client.post(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 302
    assert response.url == "https://verify.didit.me/session/sess-abc"

    verification = DiditVerification.objects.get(distributor=distributor)
    assert verification.session_id == "sess-abc"
    assert verification.status == DiditVerification.Status.PENDING

    call_kwargs = mock_create_session.call_args.kwargs
    assert call_kwargs["vendor_data"] == distributor.pk
    assert "kyc/callback" in call_kwargs["callback_url"]


@pytest.mark.django_db
@patch("apps.distributors.views.create_didit_session")
def test_post_shows_an_error_when_didit_fails(mock_create_session, client):
    distributor = _make_distributor()
    _login(client, distributor)
    mock_create_session.side_effect = DiditError("timed out")

    response = client.post(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 200
    assert not DiditVerification.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result")
def test_callback_consumes_the_session_id(mock_consume, client):
    response = client.get(
        reverse("distributors:kyc_verification_callback"),
        {"verificationSessionId": "sess-abc", "status": "Approved"},
    )

    assert response.status_code == 200
    mock_consume.assert_called_once_with("sess-abc")


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result")
def test_webhook_with_valid_signature_consumes_the_result(mock_consume, client):
    session_id, status, created_at = "sess-abc", "Approved", "2026-07-13T10:00:00Z"
    body = json.dumps(
        {"session_id": session_id, "status": status, "created_at": created_at}
    ).encode()
    message = f"{session_id}|{status}|{created_at}".encode("utf-8")
    signature = hmac.new(b"whsec_fake", message, hashlib.sha256).hexdigest()

    response = client.post(
        reverse("distributors:didit_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE_SIMPLE=signature,
    )

    assert response.status_code == 200
    mock_consume.assert_called_once_with(session_id)


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result")
def test_webhook_with_an_invalid_signature_is_rejected(mock_consume, client):
    body = json.dumps(
        {
            "session_id": "sess-abc",
            "status": "Approved",
            "created_at": "2026-07-13T10:00:00Z",
        }
    ).encode()

    response = client.post(
        reverse("distributors:didit_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE_SIMPLE="not-the-right-signature",
    )

    assert response.status_code == 400
    mock_consume.assert_not_called()


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
def test_webhook_with_malformed_json_returns_400_not_500(client):
    response = client.post(
        reverse("distributors:didit_webhook"),
        data=b"not valid json{{{",
        content_type="application/json",
        HTTP_X_SIGNATURE_SIMPLE="whatever",
    )

    assert response.status_code == 400
