import hashlib
import hmac
import json
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

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


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_webhook_with_valid_signature_and_charge_success_creates_the_account(
    mock_verify, client
):
    sponsor = _make_sponsor()
    _register(client, sponsor)
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    pending.payment_reference = "ref-webhook-1"
    pending.fee_amount_pesewas = 10000
    pending.save(update_fields=["payment_reference", "fee_amount_pesewas"])
    mock_verify.return_value = {
        "status": "success",
        "amount": 10000,
        "currency": "GHS",
    }

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "ref-webhook-1"}}
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
    pending.payment_reference = "ref-webhook-2"
    pending.save(update_fields=["payment_reference"])

    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "ref-webhook-2"}}
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
