import hashlib
import hmac
from unittest.mock import Mock, patch

from django.test import override_settings

import pytest
import requests

from apps.distributors.models import Distributor
from apps.distributors.paystack import (
    MOBILE_MONEY_BANK_CODES,
    PaystackError,
    create_transfer_recipient,
    initialize_transaction,
    initiate_transfer,
    list_banks,
    verify_transaction,
    verify_transfer,
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


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_initialize_transaction_returns_the_parsed_data(mock_post):
    mock_post.return_value = _fake_response(
        {
            "status": True,
            "message": "Authorization URL created",
            "data": {
                "authorization_url": "https://checkout.paystack.com/abc123",
                "access_code": "abc123",
                "reference": "ref123",
            },
        }
    )

    result = initialize_transaction(
        email="kofi@example.test",
        amount_pesewas=10000,
        reference="ref123",
        callback_url="https://bancostore.test/callback/",
    )

    assert result["authorization_url"] == "https://checkout.paystack.com/abc123"
    assert result["reference"] == "ref123"

    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["json"]["email"] == "kofi@example.test"
    assert call_kwargs["json"]["amount"] == "10000"
    assert call_kwargs["json"]["reference"] == "ref123"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_initialize_transaction_raises_paystack_error_on_http_failure(mock_post):
    mock_post.return_value = _fake_response({"status": False}, status_code=401)

    with pytest.raises(PaystackError):
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_initialize_transaction_raises_paystack_error_on_network_failure(mock_post):
    mock_post.side_effect = requests.ConnectionError("network down")

    with pytest.raises(PaystackError):
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transaction_returns_the_parsed_data(mock_get):
    mock_get.return_value = _fake_response(
        {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": "success",
                "reference": "ref123",
                "amount": 10000,
                "currency": "GHS",
            },
        }
    )

    result = verify_transaction("ref123")

    assert result["status"] == "success"
    assert result["amount"] == 10000
    call_args, call_kwargs = mock_get.call_args
    assert "ref123" in call_args[0]
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transaction_raises_paystack_error_on_http_failure(mock_get):
    mock_get.return_value = _fake_response({"status": False}, status_code=404)

    with pytest.raises(PaystackError):
        verify_transaction("ref123")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_list_banks_returns_the_parsed_data(mock_get):
    mock_get.return_value = _fake_response(
        {
            "status": True,
            "message": "Banks retrieved",
            "data": [
                {"name": "MTN", "code": "MTN", "slug": "mtn-mobile-money"},
                {"name": "Vodafone", "code": "VOD", "slug": "vod-mobile-money"},
            ],
        }
    )

    result = list_banks(country="ghana", currency="GHS", bank_type="mobile_money")

    assert result[0]["code"] == "MTN"
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["params"] == {
        "country": "ghana",
        "currency": "GHS",
        "type": "mobile_money",
    }
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_list_banks_raises_paystack_error_on_http_failure(mock_get):
    mock_get.return_value = _fake_response({"status": False}, status_code=401)

    with pytest.raises(PaystackError):
        list_banks(country="ghana", currency="GHS", bank_type="mobile_money")


def test_mobile_money_bank_codes_covers_every_network_choice():
    """Confirmed via a real call to list_banks() against Paystack's
    test-mode API (2026-07-23), not guessed -- see ADR-0004. Every
    Distributor.MobileMoneyNetwork choice must have an entry, or
    create_transfer_recipient would silently have no bank_code to use
    for a distributor on that network."""
    for network, _ in Distributor.MobileMoneyNetwork.choices:
        assert network in MOBILE_MONEY_BANK_CODES

    assert MOBILE_MONEY_BANK_CODES[Distributor.MobileMoneyNetwork.MTN] == "MTN"
    assert MOBILE_MONEY_BANK_CODES[Distributor.MobileMoneyNetwork.AIRTELTIGO] == "ATL"
    # Paystack's own bank list still labels this network "Vodafone" despite
    # the real-world Vodafone-to-Telecel rebrand -- VOD is still correct.
    assert MOBILE_MONEY_BANK_CODES[Distributor.MobileMoneyNetwork.TELECEL] == "VOD"


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_create_transfer_recipient_returns_the_parsed_data(mock_post):
    mock_post.return_value = _fake_response(
        {
            "status": True,
            "message": "Recipient created",
            "data": {
                "recipient_code": "RCP_abc123",
                "type": "mobile_money",
                "name": "Kofi Owusu",
                "details": {
                    "account_number": "0244123456",
                    "bank_code": "MTN",
                    "bank_name": "MTN",
                },
            },
        },
        status_code=201,
    )

    result = create_transfer_recipient(
        name="Kofi Owusu",
        account_number="0244123456",
        bank_code="MTN",
        currency="GHS",
    )

    assert result["recipient_code"] == "RCP_abc123"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["type"] == "mobile_money"
    assert call_kwargs["json"]["name"] == "Kofi Owusu"
    assert call_kwargs["json"]["account_number"] == "0244123456"
    assert call_kwargs["json"]["bank_code"] == "MTN"
    assert call_kwargs["json"]["currency"] == "GHS"
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_create_transfer_recipient_raises_paystack_error_on_http_failure(mock_post):
    mock_post.return_value = _fake_response({"status": False}, status_code=400)

    with pytest.raises(PaystackError):
        create_transfer_recipient(
            name="Kofi Owusu",
            account_number="0244123456",
            bank_code="MTN",
            currency="GHS",
        )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_initiate_transfer_returns_the_parsed_data(mock_post):
    mock_post.return_value = _fake_response(
        {
            "status": True,
            "message": "Transfer has been queued",
            "data": {
                "reference": "withdrawal-42",
                "transfer_code": "TRF_xyz789",
                "status": "pending",
                "amount": 49500,
            },
        }
    )

    result = initiate_transfer(
        amount_pesewas=49500,
        recipient_code="RCP_abc123",
        reference="withdrawal-42",
        reason="Bancostore withdrawal payout",
    )

    assert result["transfer_code"] == "TRF_xyz789"
    assert result["status"] == "pending"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["source"] == "balance"
    assert call_kwargs["json"]["amount"] == "49500"
    assert call_kwargs["json"]["recipient"] == "RCP_abc123"
    assert call_kwargs["json"]["reference"] == "withdrawal-42"
    assert call_kwargs["json"]["reason"] == "Bancostore withdrawal payout"
    assert call_kwargs["json"]["currency"] == "GHS"
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_initiate_transfer_raises_paystack_error_on_http_failure(mock_post):
    mock_post.return_value = _fake_response({"status": False}, status_code=400)

    with pytest.raises(PaystackError):
        initiate_transfer(
            amount_pesewas=49500,
            recipient_code="RCP_abc123",
            reference="withdrawal-42",
        )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_returns_the_parsed_data(mock_get):
    mock_get.return_value = _fake_response(
        {
            "status": True,
            "message": "Transfer retrieved",
            "data": {
                "reference": "withdrawal-42",
                "transfer_code": "TRF_xyz789",
                "status": "success",
                "amount": 49500,
            },
        }
    )

    result = verify_transfer("withdrawal-42")

    assert result["status"] == "success"
    assert result["transfer_code"] == "TRF_xyz789"
    call_args, call_kwargs = mock_get.call_args
    assert "withdrawal-42" in call_args[0]
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_raises_paystack_error_on_http_failure(mock_get):
    mock_get.return_value = _fake_response({"status": False}, status_code=404)

    with pytest.raises(PaystackError):
        verify_transfer("withdrawal-42")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
def test_verify_webhook_signature_accepts_a_correctly_computed_signature():
    raw_body = b'{"event": "charge.success", "data": {"reference": "ref123"}}'
    correct_signature = hmac.new(b"sk_test_fake", raw_body, hashlib.sha512).hexdigest()

    assert verify_webhook_signature(raw_body, correct_signature) is True


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
def test_verify_webhook_signature_rejects_a_wrong_signature():
    raw_body = b'{"event": "charge.success", "data": {"reference": "ref123"}}'

    assert verify_webhook_signature(raw_body, "not-the-right-signature") is False


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
def test_verify_webhook_signature_rejects_a_missing_header():
    raw_body = b'{"event": "charge.success"}'

    assert verify_webhook_signature(raw_body, "") is False
    assert verify_webhook_signature(raw_body, None) is False


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
def test_verify_webhook_signature_rejects_a_tampered_body():
    """The signature must be computed over the exact raw body Paystack sent
    -- if the body is altered after the signature was issued (e.g. someone
    intercepting and modifying the payload), verification must fail."""
    original_body = b'{"event": "charge.success", "data": {"amount": 10000}}'
    signature = hmac.new(b"sk_test_fake", original_body, hashlib.sha512).hexdigest()
    tampered_body = b'{"event": "charge.success", "data": {"amount": 1}}'

    assert verify_webhook_signature(tampered_body, signature) is False
