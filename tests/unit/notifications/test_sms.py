from unittest.mock import MagicMock, patch

from django.test import override_settings

import pytest
import requests
from constance import config

from apps.notifications.sms import (
    SmsOutOfCredit,
    SmsSendError,
    get_sms_credit_balance,
    send_sms,
)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY="test-key", MNOTIFY_SENDER_ID="Bancostore")
@patch("apps.notifications.sms.requests.post")
def test_uses_the_server_default_sender_when_sender_name_is_blank(mock_post):
    mock_post.return_value = MagicMock(raise_for_status=lambda: None)
    config.SENDER_NAME = ""

    send_sms("+233241234567", "Hello")

    assert mock_post.call_args.kwargs["json"]["sender"] == "Bancostore"


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY="test-key", MNOTIFY_SENDER_ID="Bancostore")
@patch("apps.notifications.sms.requests.post")
def test_uses_the_live_admin_configured_sender_name_when_set(mock_post):
    mock_post.return_value = MagicMock(raise_for_status=lambda: None)
    config.SENDER_NAME = "BancoShop"

    send_sms("+233241234567", "Hello")

    assert mock_post.call_args.kwargs["json"]["sender"] == "BancoShop"


# --- Task 63a: failures are clear, and never leak the API key ---------------
#
# mNotify takes its API key in the URL (?key=...). requests puts the full URL
# into HTTPError and ConnectionError messages, and every SMS send site logs
# failures with logger.exception -- so before this, production's error logs
# held the live key on every failed send (seen on 2026-09-17).

LEAK_MARKER = "leakcheck-7f3a"


def _http_error_response(status):
    response = MagicMock(status_code=status)
    response.raise_for_status.side_effect = requests.HTTPError(
        f"{status} Client Error for url: https://api.mnotify.com/api/sms/quick"
        f"?key={LEAK_MARKER}"
    )
    return response


def _assert_key_not_in(exc_info):
    """The key must be absent from the error AND from anything chained to
    it, since logger.exception prints the whole chain."""
    exc = exc_info.value
    seen = []
    while exc is not None:
        seen.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    for e in seen:
        assert LEAK_MARKER not in str(e)
        assert LEAK_MARKER not in repr(e.args)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.post")
def test_out_of_credit_is_its_own_error(mock_post):
    mock_post.return_value = _http_error_response(402)

    with pytest.raises(SmsOutOfCredit) as exc_info:
        send_sms("+233241234567", "Hello")

    _assert_key_not_in(exc_info)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.post")
def test_another_http_failure_raises_without_the_key(mock_post):
    mock_post.return_value = _http_error_response(500)

    with pytest.raises(SmsSendError) as exc_info:
        send_sms("+233241234567", "Hello")

    assert not isinstance(exc_info.value, SmsOutOfCredit)
    assert "500" in str(exc_info.value)
    _assert_key_not_in(exc_info)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.post")
def test_a_network_failure_raises_without_the_key(mock_post):
    mock_post.side_effect = requests.ConnectionError(
        f"Max retries exceeded with url: /api/sms/quick?key={LEAK_MARKER}"
    )

    with pytest.raises(SmsSendError) as exc_info:
        send_sms("+233241234567", "Hello")

    _assert_key_not_in(exc_info)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.get")
def test_the_credit_balance_counts_bonus_credits_too(mock_get):
    """Shape verified live against production on 2026-09-17:
    {"status":"success","wallet":"59.3","balance":0,"bonus":0}. `wallet` is
    money (GHS) not yet converted to credits, so it is not counted."""
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "success",
            "wallet": "59.3",
            "balance": 150,
            "bonus": 30,
        },
    )

    assert get_sms_credit_balance() == 180
    assert mock_get.call_args.args[0] == "https://api.mnotify.com/api/balance/sms"


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.get")
def test_an_unreadable_balance_response_raises_without_the_key(mock_get):
    mock_get.side_effect = requests.ConnectionError(
        f"url: /api/balance/sms?key={LEAK_MARKER}"
    )

    with pytest.raises(SmsSendError) as exc_info:
        get_sms_credit_balance()

    _assert_key_not_in(exc_info)


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY=LEAK_MARKER)
@patch("apps.notifications.sms.requests.get")
def test_a_balance_response_without_a_number_is_an_error(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"status": "error", "message": "Invalid key"}
    )

    with pytest.raises(SmsSendError):
        get_sms_credit_balance()


@override_settings(MNOTIFY_API_KEY="")
def test_there_is_no_balance_to_check_without_a_key():
    """Local dev and tests use the fake sender, which has no credit."""
    assert get_sms_credit_balance() is None
