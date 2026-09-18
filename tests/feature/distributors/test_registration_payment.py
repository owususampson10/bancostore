import hashlib
import hmac
import json
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.distributors.models import Distributor, PendingRegistration

User = get_user_model()

_phone_seq = count(1)


def _make_sponsor():
    phone = f"+233209{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, ir_id="IR00001")


VALID_REGISTRATION_DATA = {
    "full_name": "Kofi Mensah",
    "email": "kofi@example.test",
    "address": "12 Ring Road",
    "area": "Osu",
    "landmark": "Near the market",
    "password1": "S3cure-Passw0rd!",
    "password2": "S3cure-Passw0rd!",
    "terms_accepted": "on",
}


def _register(client, sponsor, phone="+233241234567"):
    return client.post(
        reverse("distributors:register"),
        {
            **VALID_REGISTRATION_DATA,
            "phone_number": phone,
            "sponsor_ir_id": sponsor.ir_id,
        },
    )


@pytest.mark.django_db
@patch("apps.distributors.views.initialize_transaction")
def test_pay_registration_fee_without_a_session_token_redirects_to_register(
    mock_init, client
):
    response = client.get(reverse("distributors:pay_registration_fee"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:register")
    mock_init.assert_not_called()


@pytest.mark.django_db
@patch("apps.distributors.views.initialize_transaction")
def test_pay_registration_fee_initializes_and_redirects_to_authorization_url(
    mock_init, client
):
    sponsor = _make_sponsor()
    _register(client, sponsor)
    mock_init.return_value = {
        "authorization_url": "https://checkout.paystack.com/xyz",
        "access_code": "xyz",
        "reference": "whatever-paystack-echoes-back",
    }

    response = client.get(reverse("distributors:pay_registration_fee"))

    assert response.status_code == 302
    assert response.url == "https://checkout.paystack.com/xyz"

    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    assert pending.fee_amount_pesewas == 10000  # REGISTRATION_FEE default GHS 100
    assert pending.payment_reference

    call_kwargs = mock_init.call_args.kwargs
    assert call_kwargs["reference"] == pending.payment_reference
    assert call_kwargs["amount_pesewas"] == 10000


@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_registration")
def test_callback_view_consumes_the_reference_from_the_query_string(
    mock_consume, client
):
    response = client.get(
        reverse("distributors:registration_payment_callback"),
        {"reference": "ref-abc"},
    )

    assert response.status_code == 200
    mock_consume.assert_called_once_with("ref-abc")


@pytest.mark.django_db
def test_callback_auto_logs_in_when_session_matches_a_consumed_registration(client):
    """Security-critical: auto-login must be tied to the browser session
    that started registration, not just to possessing a reference string
    (references can leak via browser history/referrer). This test uses the
    real register() -> session flow, then simulates the webhook having
    already consumed it (as it would, arriving before or after the
    callback), before hitting the callback."""
    sponsor = _make_sponsor()
    _register(client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "ref-already-consumed"
    pending.save(update_fields=["payment_reference"])

    # Simulate the webhook having already created the account.
    user = User(username="+233241234567", email=pending.email)
    user.password = make_password("S3cure-Passw0rd!")
    user.save()
    Distributor.objects.create(
        user=user,
        phone_number="+233241234567",
        full_name=pending.full_name,
        sponsor=sponsor,
    )
    pending.consumed_at = timezone.now()
    pending.save(update_fields=["consumed_at"])

    with patch("apps.distributors.views.consume_paid_registration"):
        response = client.get(
            reverse("distributors:registration_payment_callback"),
            {"reference": "ref-already-consumed"},
        )

    assert response.status_code == 302
    assert response.url == reverse("distributors:select_starter_pack")
    assert int(client.session["_auth_user_id"]) == user.id


@pytest.mark.django_db
def test_callback_does_not_auto_login_when_registration_is_not_yet_consumed(client):
    sponsor = _make_sponsor()
    _register(client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "ref-not-yet"
    pending.save(update_fields=["payment_reference"])

    with patch("apps.distributors.views.consume_paid_registration"):
        response = client.get(
            reverse("distributors:registration_payment_callback"),
            {"reference": "ref-not-yet"},
        )

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_callback_does_not_auto_login_without_a_matching_session_token(client):
    """A different browser/session (e.g. someone who obtained a leaked
    reference) must never get auto-logged into the account, even if that
    exact reference did resolve to a genuinely confirmed registration."""
    sponsor = _make_sponsor()
    other_client = Client()
    _register(other_client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "ref-belongs-to-other-session"
    pending.consumed_at = timezone.now()
    pending.save(update_fields=["payment_reference", "consumed_at"])
    user = User.objects.create_user(username="+233241234567", password="x")
    Distributor.objects.create(user=user, phone_number="+233241234567", sponsor=sponsor)

    # `client` (this test's own fixture) never registered -- no
    # pending_registration_token in its session.
    with patch("apps.distributors.views.consume_paid_registration"):
        response = client.get(
            reverse("distributors:registration_payment_callback"),
            {"reference": "ref-belongs-to-other-session"},
        )

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_webhook_with_valid_signature_and_charge_success_creates_the_account(
    mock_verify, client
):
    sponsor = _make_sponsor()
    _register(client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "reg-webhook-1"
    pending.fee_amount_pesewas = 10000
    pending.save(update_fields=["payment_reference", "fee_amount_pesewas"])
    mock_verify.return_value = {
        "status": "success",
        "amount": 10000,
        "currency": "GHS",
    }

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "reg-webhook-1"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    pending.refresh_from_db()
    assert pending.consumed_at is not None
    assert Distributor.objects.filter(phone_number="+233241234567").exists()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
def test_webhook_with_an_invalid_signature_is_rejected(client):
    sponsor = _make_sponsor()
    _register(client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "reg-webhook-2"
    pending.save(update_fields=["payment_reference"])

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "reg-webhook-2"}}
    ).encode()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE="not-the-right-signature",
    )

    assert response.status_code == 400
    pending.refresh_from_db()
    assert pending.consumed_at is None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_with_a_different_event_type_is_a_noop(mock_consume, client):
    body = json.dumps(
        {"event": "transfer.success", "data": {"reference": "irrelevant"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    mock_consume.assert_not_called()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
def test_webhook_with_malformed_json_returns_400_not_500(client):
    body = b"not valid json{{{"
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 400


# --- Task 67 ------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.distributors.views.initialize_transaction")
def test_pay_registration_fee_records_when_checkout_opened_and_sends_payer_details(
    mock_init, client
):
    """The payer's name and phone travel with the Paystack transaction, so a
    payment can still be tied to a person if the pending registration is
    gone by the time it arrives. Never the password or address."""
    _register(client, _make_sponsor())
    mock_init.return_value = {"authorization_url": "https://checkout.paystack.com/x"}

    client.get(reverse("distributors:pay_registration_fee"))

    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    assert pending.payment_initialized_at is not None
    metadata = mock_init.call_args.kwargs["metadata"]
    assert metadata["full_name"] == "Kofi Mensah"
    assert metadata["phone_number"] == "+233241234567"
    assert "password" not in json.dumps(metadata).lower()
    assert "Ring Road" not in json.dumps(metadata)


def _signed_webhook(client, reference):
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": reference}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()
    return client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_asks_paystack_to_retry_when_verification_failed(mock_consume, client):
    """Paystack only redelivers a webhook that did not get a 2xx. Answering
    200 after a verify timeout threw away the one push we get."""
    from apps.distributors.services import RegistrationPaymentOutcome

    mock_consume.return_value = RegistrationPaymentOutcome.VERIFY_FAILED

    response = _signed_webhook(client, "reg-anything")

    assert response.status_code == 503


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_answers_200_for_every_definite_outcome(mock_consume, client):
    from apps.distributors.services import RegistrationPaymentOutcome

    for outcome in RegistrationPaymentOutcome:
        if outcome is RegistrationPaymentOutcome.VERIFY_FAILED:
            continue
        mock_consume.return_value = outcome
        assert _signed_webhook(client, "reg-anything").status_code == 200


@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_registration")
def test_callback_is_rate_limited_per_ip(mock_consume, client):
    """Public, and every call can cost an authenticated Paystack request."""
    url = reverse("distributors:registration_payment_callback")
    statuses = [client.get(url, {"reference": "ref-x"}).status_code for _ in range(61)]

    assert statuses[:60] == [200] * 60
    assert statuses[60] != 200
