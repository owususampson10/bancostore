from unittest.mock import MagicMock, patch

from django.test import override_settings

import pytest
from constance import config

from apps.notifications.sms import send_sms


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
