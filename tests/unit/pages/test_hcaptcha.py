from unittest.mock import Mock, patch

import requests

from apps.pages.hcaptcha import hcaptcha_enabled, verify_hcaptcha


class TestHcaptchaEnabled:
    def test_disabled_when_both_keys_blank(self, settings):
        settings.HCAPTCHA_SITE_KEY = ""
        settings.HCAPTCHA_SECRET_KEY = ""

        assert hcaptcha_enabled() is False

    def test_disabled_when_only_site_key_set(self, settings):
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = ""

        assert hcaptcha_enabled() is False

    def test_enabled_when_both_keys_set(self, settings):
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = "secret-key"

        assert hcaptcha_enabled() is True


class TestVerifyHcaptcha:
    def test_passes_when_disabled(self, settings):
        """Blank keys locally/CI must never block a real submission --
        same fallback convention as MNOTIFY_API_KEY's fake sender."""
        settings.HCAPTCHA_SITE_KEY = ""
        settings.HCAPTCHA_SECRET_KEY = ""

        assert verify_hcaptcha("") is True

    def test_rejects_missing_token_when_enabled(self, settings):
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = "secret-key"

        assert verify_hcaptcha("") is False

    def test_accepts_a_successful_verification(self, settings):
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = "secret-key"
        mock_response = Mock()
        mock_response.json.return_value = {"success": True}

        with patch("apps.pages.hcaptcha.requests.post", return_value=mock_response):
            assert verify_hcaptcha("a-real-token") is True

    def test_rejects_a_failed_verification(self, settings):
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = "secret-key"
        mock_response = Mock()
        mock_response.json.return_value = {"success": False}

        with patch("apps.pages.hcaptcha.requests.post", return_value=mock_response):
            assert verify_hcaptcha("a-fake-token") is False

    def test_fails_closed_on_a_network_error(self, settings):
        """Unlike Paystack's wrapper (which raises), a CAPTCHA check that
        can't be confirmed must reject, not silently let the submission
        through as if it passed."""
        settings.HCAPTCHA_SITE_KEY = "site-key"
        settings.HCAPTCHA_SECRET_KEY = "secret-key"

        with patch(
            "apps.pages.hcaptcha.requests.post",
            side_effect=requests.RequestException("timed out"),
        ):
            assert verify_hcaptcha("a-real-token") is False
