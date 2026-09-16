import hashlib
import hmac
import json
from unittest.mock import Mock, patch

from django.test import override_settings

import pytest
import requests

from apps.distributors.models import Distributor
from apps.distributors.paystack import (
    MOBILE_MONEY_BANK_CODES,
    PaystackError,
    PaystackNotFoundError,
    create_transfer_recipient,
    initialize_transaction,
    initiate_transfer,
    list_banks,
    paystack_customer_email,
    verify_transaction,
    verify_transfer,
    verify_webhook_signature,
)


def _fake_response(json_data, status_code=200, text=None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    # Task 59: a real requests.Response always exposes .text -- without
    # setting it here, a Mock hands back an auto-created attribute and
    # _raise_as_paystack_error's body capture could never be tested
    # against anything resembling real behavior. Defaults to the JSON
    # body's own serialized form, which is what Paystack really sends.
    response.text = json.dumps(json_data) if text is None else text
    if status_code >= 400:
        # response=response, matching requests.Response.raise_for_status()'s
        # real behavior exactly -- without this, exc.response is None
        # regardless of status_code, and _raise_as_paystack_error's
        # 404-vs-everything-else distinction could never be tested for
        # real (a bug in the test double, not just an omission).
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error", response=response
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
                "reference": "withdrawal-00000042",
                "transfer_code": "TRF_xyz789",
                "status": "pending",
                "amount": 49500,
            },
        }
    )

    result = initiate_transfer(
        amount_pesewas=49500,
        recipient_code="RCP_abc123",
        reference="withdrawal-00000042",
        reason="Bancostore withdrawal payout",
    )

    assert result["transfer_code"] == "TRF_xyz789"
    assert result["status"] == "pending"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["source"] == "balance"
    assert call_kwargs["json"]["amount"] == "49500"
    assert call_kwargs["json"]["recipient"] == "RCP_abc123"
    assert call_kwargs["json"]["reference"] == "withdrawal-00000042"
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
            reference="withdrawal-00000042",
        )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_returns_the_parsed_data(mock_get):
    mock_get.return_value = _fake_response(
        {
            "status": True,
            "message": "Transfer retrieved",
            "data": {
                "reference": "withdrawal-00000042",
                "transfer_code": "TRF_xyz789",
                "status": "success",
                "amount": 49500,
            },
        }
    )

    result = verify_transfer("withdrawal-00000042")

    assert result["status"] == "success"
    assert result["transfer_code"] == "TRF_xyz789"
    call_args, call_kwargs = mock_get.call_args
    assert "withdrawal-00000042" in call_args[0]
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk_test_fake"
    assert call_kwargs["timeout"] is not None


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_raises_paystack_error_on_http_failure(mock_get):
    mock_get.return_value = _fake_response({"status": False}, status_code=404)

    with pytest.raises(PaystackError):
        verify_transfer("withdrawal-00000042")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_raises_not_found_specifically_on_404(mock_get):
    """Task 16f needs to distinguish "Paystack has never seen this
    reference" (404) from every other failure -- that's what lets a
    payout retry safely fall back to initiate_transfer instead of
    treating a transient error the same way."""
    mock_get.return_value = _fake_response({"status": False}, status_code=404)

    with pytest.raises(PaystackNotFoundError):
        verify_transfer("withdrawal-00000042")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_verify_transfer_does_not_raise_not_found_on_a_non_404_failure(mock_get):
    """A 500, a timeout, or any other failure must NOT be treated as
    "reference not found" -- doing so would risk a real double-initiate
    if the transfer actually does exist and verification merely failed
    for an unrelated, possibly transient reason."""
    mock_get.return_value = _fake_response({"status": False}, status_code=500)

    with pytest.raises(PaystackError) as exc_info:
        verify_transfer("withdrawal-00000042")

    assert not isinstance(exc_info.value, PaystackNotFoundError)


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


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_paystack_error_message_includes_the_response_body(mock_post):
    """Task 59: a real live-key checkout failure on production could not
    be diagnosed from the log, because requests.HTTPError's string form
    is only ever "401 Client Error: Unauthorized for url: ..." -- the
    reason Paystack actually rejected the call lives in the response
    body and was being dropped entirely."""
    mock_post.return_value = _fake_response(
        {"status": False, "message": "Invalid key"}, status_code=401
    )

    with pytest.raises(PaystackError) as exc_info:
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )

    assert "Invalid key" in str(exc_info.value)


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.get")
def test_paystack_error_message_includes_the_response_body_for_a_404(mock_get):
    """The 404 branch raises a different exception class from a separate
    line, so it needs its own proof that the body survives -- only one of
    the two raise sites being right is a real possibility."""
    mock_get.return_value = _fake_response(
        {"status": False, "message": "Transaction not found"}, status_code=404
    )

    with pytest.raises(PaystackNotFoundError) as exc_info:
        verify_transaction("ref123")

    assert "Transaction not found" in str(exc_info.value)


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_paystack_error_message_truncates_an_oversized_response_body(mock_post):
    """Not every failure body is Paystack's own small JSON -- a proxy or
    gateway in front of the API can return a full HTML error page, and an
    unbounded body would go into the production log on every failure."""
    mock_post.return_value = _fake_response(
        {"status": False}, status_code=502, text="<html>" + ("x" * 5000) + "</html>"
    )

    with pytest.raises(PaystackError) as exc_info:
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )

    message = str(exc_info.value)
    assert "truncated" in message
    assert len(message) < 1000


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_paystack_error_still_raises_when_there_is_no_response_at_all(mock_post):
    """A timeout has no response to read a body from. Capturing the body
    must not turn "we could not reach Paystack" into an AttributeError
    that masks the real failure."""
    mock_post.side_effect = requests.Timeout("connection timed out")

    with pytest.raises(PaystackError) as exc_info:
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )

    assert "connection timed out" in str(exc_info.value)


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@patch("apps.distributors.paystack.requests.post")
def test_paystack_error_message_omits_an_empty_response_body(mock_post):
    """An empty body should not leave a dangling "response body:" label
    with nothing after it in the log."""
    mock_post.return_value = _fake_response({}, status_code=500, text="   ")

    with pytest.raises(PaystackError) as exc_info:
        initialize_transaction(
            email="kofi@example.test",
            amount_pesewas=10000,
            reference="ref123",
            callback_url="https://bancostore.test/callback/",
        )

    assert "response body" not in str(exc_info.value)


def test_paystack_customer_email_uses_the_real_email_when_there_is_one():
    assert (
        paystack_customer_email("kofi@example.com", "+233241234567")
        == "kofi@example.com"
    )


def test_paystack_customer_email_treats_a_whitespace_only_email_as_absent():
    """Order.email is blank=True with a "" default, but a form value of
    "   " would otherwise be handed to Paystack verbatim as an address."""
    result = paystack_customer_email("   ", "+233241234567")

    assert result != "   "
    assert result.endswith("@guests.bancostore.com")


def test_paystack_customer_email_fallback_contains_no_plus_sign():
    """Task 59, confirmed against the live API: a blank email produced
    "+233241234567@bancostore.test", and Paystack's LIVE mode answers
    that payload with 400 Bad Request (test mode accepted it, which is
    why this survived to production). PhoneNumberField stores E.164, so
    the "+" is always there unless it is stripped deliberately."""
    result = paystack_customer_email("", "+233241234567")

    assert "+" not in result
    assert "233241234567" in result


def test_paystack_customer_email_fallback_uses_a_resolvable_domain():
    """ ".test" is a reserved TLD (RFC 2606) that resolves nowhere. Either
    it or the "+" could have been what Paystack rejected -- the fallback
    avoids both rather than guessing which one mattered."""
    result = paystack_customer_email("", "+233241234567")

    assert ".test" not in result
    assert result.endswith("@guests.bancostore.com")


def test_paystack_customer_email_fallback_is_distinct_per_phone_number():
    """A single shared address for every guest would collapse them into
    one customer on Paystack's own dashboard, making reconciliation of
    real orders impossible."""
    first = paystack_customer_email("", "+233241234567")
    second = paystack_customer_email("", "+233209876543")

    assert first != second


def test_paystack_customer_email_fallback_survives_a_phone_with_no_digits():
    """Every current caller has a required phone field, so this cannot
    happen today -- but returning "guest-@guests.bancostore.com" if one ever
    changed would be a malformed address, which is the exact class of
    bug this function exists to stop."""
    result = paystack_customer_email("", "")

    assert result == "guest@guests.bancostore.com"
