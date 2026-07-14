import hashlib
import hmac
import json
import time
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
def test_distributor_with_a_submitted_verification_sees_a_pending_review_message(
    client,
):
    """A distributor who already went through Didit's flow but hasn't been
    approved/rejected by an admin yet must NOT see the same "Start
    Verification" screen again -- nothing on that screen would tell them
    they've already submitted, so they'd be confused about whether they
    need to redo it."""
    distributor = _make_distributor()
    DiditVerification.objects.create(
        distributor=distributor,
        session_id="sess-already-submitted",
        status=DiditVerification.Status.IN_REVIEW,
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:start_kyc_verification"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Start Verification" not in body
    assert "pending" in body.lower() or "review" in body.lower()


@pytest.mark.django_db
def test_distributor_with_no_submission_yet_sees_the_start_button(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 200
    assert "Start Verification" in response.content.decode()


@pytest.mark.django_db
def test_rejected_distributor_can_still_see_the_start_button_to_retry(client):
    """Didit's result is purely informational -- only an explicit admin
    rejection blocks kyc_status, and even then a distributor must be able
    to restart (existing behavior, unchanged by this pending-review
    status page)."""
    distributor = _make_distributor()
    distributor.kyc_status = Distributor.KycStatus.REJECTED
    distributor.save(update_fields=["kyc_status"])
    DiditVerification.objects.create(
        distributor=distributor,
        session_id="sess-declined",
        status=DiditVerification.Status.DECLINED,
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 200
    assert "Start Verification" in response.content.decode()


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


def _v2_signature(payload, secret=b"whsec_fake"):
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result_task.delay")
def test_webhook_with_valid_signature_enqueues_the_consume_task(mock_delay, client):
    """Didit's own docs require a response within 5 seconds, or the
    delivery is retried and eventually dropped -- so the webhook must
    enqueue the (potentially slow) consume work rather than run it inline.
    See apps/distributors/tasks.py::consume_didit_result_task."""
    payload = {"session_id": "sess-abc", "status": "Approved"}
    body = json.dumps(payload).encode()
    signature = _v2_signature(payload)
    timestamp = str(int(time.time()))

    response = client.post(
        reverse("distributors:didit_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE_V2=signature,
        HTTP_X_TIMESTAMP=timestamp,
    )

    assert response.status_code == 200
    mock_delay.assert_called_once_with("sess-abc")


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result_task.delay")
def test_webhook_with_an_invalid_signature_is_rejected(mock_delay, client):
    body = json.dumps({"session_id": "sess-abc", "status": "Approved"}).encode()

    response = client.post(
        reverse("distributors:didit_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE_V2="not-the-right-signature",
        HTTP_X_TIMESTAMP=str(int(time.time())),
    )

    assert response.status_code == 400
    mock_delay.assert_not_called()


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_didit_result_task.delay")
def test_webhook_with_a_stale_timestamp_is_rejected(mock_delay, client):
    payload = {"session_id": "sess-abc", "status": "Approved"}
    body = json.dumps(payload).encode()
    signature = _v2_signature(payload)
    stale_timestamp = str(int(time.time()) - 301)

    response = client.post(
        reverse("distributors:didit_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE_V2=signature,
        HTTP_X_TIMESTAMP=stale_timestamp,
    )

    assert response.status_code == 400
    mock_delay.assert_not_called()


@override_settings(DIDIT_WEBHOOK_SECRET="whsec_fake")
@pytest.mark.django_db
def test_webhook_with_malformed_json_returns_400_not_500(client):
    response = client.post(
        reverse("distributors:didit_webhook"),
        data=b"not valid json{{{",
        content_type="application/json",
        HTTP_X_SIGNATURE_V2="whatever",
        HTTP_X_TIMESTAMP=str(int(time.time())),
    )

    assert response.status_code == 400
