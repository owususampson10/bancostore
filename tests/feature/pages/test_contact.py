from unittest.mock import patch

from django.core import mail
from django.urls import reverse

import pytest
from constance import config


def _valid_payload(**overrides):
    payload = {
        "name": "Ama Owusu",
        "email": "ama@example.test",
        "subject": "Question about a starter pack",
        "message": "Hi, I'd like to know more about the Pack B starter kit.",
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_contact_page_renders(client):
    response = client.get(reverse("pages:contact"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_contact_page_shows_only_channels_the_admin_has_actually_set(client):
    config.CONTACT_PHONE_NUMBER = "+233 24 000 0000"
    config.CONTACT_EMAIL_ADDRESS = ""
    config.WHATSAPP_SUPPORT_NUMBER = ""
    config.PHYSICAL_ADDRESS = ""

    response = client.get(reverse("pages:contact"))

    content = response.content.decode()
    assert "+233 24 000 0000" in content


@pytest.mark.django_db
def test_contact_page_shows_nothing_for_a_whitespace_only_setting(client):
    """Regression guard (doubt-driven-development finding): a constance
    value of "   " is a non-empty Python string, so a naive truthiness
    check would treat it as "the admin set this" and render a blank
    contact row. Must be stripped before the check."""
    config.CONTACT_PHONE_NUMBER = "   "
    config.CONTACT_EMAIL_ADDRESS = ""
    config.WHATSAPP_SUPPORT_NUMBER = ""
    config.PHYSICAL_ADDRESS = ""

    response = client.get(reverse("pages:contact"))

    assert response.context["contact_phone_number"] is None


@pytest.mark.django_db
def test_contact_page_shows_no_channels_when_none_are_set(client):
    config.CONTACT_PHONE_NUMBER = ""
    config.CONTACT_EMAIL_ADDRESS = ""
    config.WHATSAPP_SUPPORT_NUMBER = ""
    config.PHYSICAL_ADDRESS = ""

    response = client.get(reverse("pages:contact"))

    assert response.context["contact_phone_number"] is None
    assert response.context["contact_email_address"] is None
    assert response.context["whatsapp_support_number"] is None
    assert response.context["physical_address"] is None


@pytest.mark.django_db
def test_valid_submission_sends_exactly_one_email_with_reply_to(client):
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    response = client.post(reverse("pages:contact"), _valid_payload(), follow=True)

    assert response.status_code == 200
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["hello@bancostore.test"]
    assert sent.reply_to == ["ama@example.test"]
    assert "Question about a starter pack" in sent.subject


@pytest.mark.django_db
def test_valid_submission_shows_a_success_message(client):
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    response = client.post(reverse("pages:contact"), _valid_payload(), follow=True)

    messages = [m.message for m in response.context["messages"]]
    assert any("sent" in m.lower() or "thank" in m.lower() for m in messages)


@pytest.mark.django_db
def test_submission_falls_back_to_default_from_email_when_unconfigured(client):
    """The contact form must still "work" (send somewhere) even before an
    admin has configured a real contact email via the settings panel."""
    config.CONTACT_EMAIL_ADDRESS = ""

    client.post(reverse("pages:contact"), _valid_payload())

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [mail.outbox[0].from_email]


@pytest.mark.django_db
def test_falling_back_to_default_from_email_logs_a_warning(client, caplog):
    """Code-review finding: this fallback is a silent black hole in
    production (nobody reads the "no-reply" inbox) unless it's logged
    somewhere an admin would actually notice."""
    import logging

    config.CONTACT_EMAIL_ADDRESS = ""

    with caplog.at_level(logging.WARNING, logger="apps.pages.views"):
        client.post(reverse("pages:contact"), _valid_payload())

    assert any("CONTACT_EMAIL_ADDRESS" in record.message for record in caplog.records)


@pytest.mark.django_db
def test_whatsapp_link_strips_a_messily_formatted_number(client):
    """Code-review finding: the admin-entered number can be in any
    common local format (dashes, parentheses, spaces) -- the rendered
    wa.me link must contain only digits, not a broken/malformed URL."""
    config.WHATSAPP_SUPPORT_NUMBER = "+233 (24) 000-0000"

    response = client.get(reverse("pages:contact"))

    assert response.context["whatsapp_link"] == "https://wa.me/233240000000"


@pytest.mark.django_db
def test_an_implausibly_long_email_is_rejected_not_sent(client):
    """Security-review finding: name/subject/message all have a
    max_length but email didn't -- an arbitrarily long (but
    regex-valid) address would otherwise reach the outgoing email's
    Reply-To header and body unbounded. No real address exceeds RFC
    5321's 254-char limit."""
    response = client.post(
        reverse("pages:contact"),
        _valid_payload(email=f"{'a' * 250}@example.test"),
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    assert response.context["form"].errors


@pytest.mark.django_db
def test_invalid_submission_shows_errors_and_sends_no_email(client):
    response = client.post(
        reverse("pages:contact"), _valid_payload(message=""), follow=True
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    assert response.context["form"].errors


@pytest.mark.django_db
def test_a_subject_containing_crlf_is_rejected_not_sent(client):
    """Defense in depth against email header injection -- Django's own
    SafeMIMEText raises BadHeaderError for a raw \\r/\\n in a header
    value, but this must be caught by form validation, not left to
    surface as an unhandled exception."""
    response = client.post(
        reverse("pages:contact"),
        _valid_payload(subject="Hello\r\nBcc: attacker@evil.test"),
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    assert response.context["form"].errors


@pytest.mark.django_db
def test_a_name_containing_crlf_is_rejected_not_sent(client):
    response = client.post(
        reverse("pages:contact"),
        _valid_payload(name="Ama\r\nBcc: attacker@evil.test"),
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    assert response.context["form"].errors


@pytest.mark.django_db
def test_a_send_failure_is_caught_shows_an_error_and_is_logged(client, caplog):
    import logging

    with patch(
        "apps.pages.views.EmailMessage.send", side_effect=RuntimeError("SMTP is down")
    ):
        with caplog.at_level(logging.ERROR, logger="apps.pages.views"):
            response = client.post(
                reverse("pages:contact"), _valid_payload(), follow=True
            )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    messages = [m.message for m in response.context["messages"]]
    assert any(
        "couldn't" in m.lower() or "error" in m.lower() or "try again" in m.lower()
        for m in messages
    )
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.django_db
def test_contact_form_post_is_rate_limited_per_ip(client):
    responses = [
        client.post(reverse("pages:contact"), _valid_payload()) for _ in range(30)
    ]

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_a_filled_honeypot_field_silently_discards_the_submission(client):
    """A bot that fills every field (including the hidden honeypot) must
    get a normal-looking success response with no email actually sent --
    it should get no signal it was caught."""
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    response = client.post(
        reverse("pages:contact"),
        _valid_payload(honeypot="I am a bot"),
        follow=True,
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    messages_text = [m.message for m in response.context["messages"]]
    assert any("sent" in m.lower() or "thank" in m.lower() for m in messages_text)


@pytest.mark.django_db
def test_an_empty_honeypot_field_sends_normally(client):
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    client.post(reverse("pages:contact"), _valid_payload(honeypot=""))

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_contact_page_renders_no_captcha_widget_when_unconfigured(client, settings):
    settings.HCAPTCHA_SITE_KEY = ""
    settings.HCAPTCHA_SECRET_KEY = ""

    response = client.get(reverse("pages:contact"))

    assert "h-captcha" not in response.content.decode()


@pytest.mark.django_db
def test_contact_page_renders_the_captcha_widget_when_configured(client, settings):
    settings.HCAPTCHA_SITE_KEY = "a-real-site-key"
    settings.HCAPTCHA_SECRET_KEY = "a-real-secret-key"

    response = client.get(reverse("pages:contact"))

    content = response.content.decode()
    assert "h-captcha" in content
    assert "a-real-site-key" in content


@pytest.mark.django_db
def test_submission_is_rejected_when_captcha_verification_fails(client, settings):
    settings.HCAPTCHA_SITE_KEY = "a-real-site-key"
    settings.HCAPTCHA_SECRET_KEY = "a-real-secret-key"
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    with patch("apps.pages.views.verify_hcaptcha", return_value=False):
        response = client.post(
            reverse("pages:contact"),
            {**_valid_payload(), "h-captcha-response": "a-bad-token"},
            follow=True,
        )

    assert response.status_code == 200
    assert len(mail.outbox) == 0
    messages_text = [m.message for m in response.context["messages"]]
    assert any("captcha" in m.lower() for m in messages_text)


@pytest.mark.django_db
def test_submission_sends_when_captcha_verification_succeeds(client, settings):
    settings.HCAPTCHA_SITE_KEY = "a-real-site-key"
    settings.HCAPTCHA_SECRET_KEY = "a-real-secret-key"
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    with patch("apps.pages.views.verify_hcaptcha", return_value=True) as mock_verify:
        client.post(
            reverse("pages:contact"),
            {**_valid_payload(), "h-captcha-response": "a-good-token"},
        )

    assert len(mail.outbox) == 1
    mock_verify.assert_called_once_with("a-good-token")


@pytest.mark.django_db
def test_submission_sends_when_captcha_is_unconfigured(client, settings):
    """No hCaptcha keys set (the local/CI default) must never block a
    real submission -- matches the disabled-by-default fallback tested
    directly in tests/unit/pages/test_hcaptcha.py."""
    settings.HCAPTCHA_SITE_KEY = ""
    settings.HCAPTCHA_SECRET_KEY = ""
    config.CONTACT_EMAIL_ADDRESS = "hello@bancostore.test"

    client.post(reverse("pages:contact"), _valid_payload())

    assert len(mail.outbox) == 1
