import pytest

from apps.notifications.models import NotificationTemplate
from apps.notifications.rendering import (
    render_email_or_default,
    render_or_default,
    render_template,
)


def test_substitutes_a_known_placeholder():
    result = render_template("Your code is {{code}}.", {"code": "123456"})

    assert result == "Your code is 123456."


def test_substitutes_multiple_placeholders():
    result = render_template(
        "GHS {{amount}} -- {{reason}}",
        {"amount": "50.00", "reason": "insufficient balance"},
    )

    assert result == "GHS 50.00 -- insufficient balance"


def test_leaves_an_unknown_placeholder_literal_instead_of_raising():
    result = render_template("Hello {{unknown_name}}.", {"code": "123456"})

    assert result == "Hello {{unknown_name}}."


def test_never_executes_or_accesses_attributes_via_the_template_string():
    """Security-review finding: a malicious admin-entered template
    string like this must never trigger attribute access, even though
    str.format(**dict)-style substitution would happily do so for a
    {0.__class__}-shaped token. Since this isn't {{ }} shaped at all,
    it's simply not recognized as a placeholder -- left as literal text."""
    malicious = "{{code.__class__.__init__.__globals__}}"

    result = render_template(malicious, {"code": "123456"})

    assert result == malicious


def test_a_template_containing_no_placeholders_passes_through_unchanged():
    result = render_template("Your KYC verification has been approved!", {})

    assert result == "Your KYC verification has been approved!"


@pytest.mark.django_db
def test_render_or_default_uses_the_live_template_row():
    # OTP_CODE is pre-seeded by migration 0006_seed_notification_templates
    # -- update the already-existing row rather than creating a second
    # one, which would collide with `key`'s unique constraint.
    NotificationTemplate.objects.filter(key=NotificationTemplate.Key.OTP_CODE).update(
        body="Custom wording: {{code}}"
    )

    result = render_or_default(
        NotificationTemplate.Key.OTP_CODE,
        {"code": "999999"},
        default_body="Fallback: {{code}}",
    )

    assert result == "Custom wording: 999999"


@pytest.mark.django_db
def test_render_or_default_falls_back_when_no_row_exists():
    """A missing row (e.g. accidentally deleted) must never break a real
    send -- falls back to the hardcoded default, still rendered against
    the same context."""
    NotificationTemplate.objects.filter(key=NotificationTemplate.Key.OTP_CODE).delete()

    result = render_or_default(
        NotificationTemplate.Key.OTP_CODE,
        {"code": "999999"},
        default_body="Your code is {{code}}.",
    )

    assert result == "Your code is 999999."


@pytest.mark.django_db
def test_render_email_or_default_uses_the_live_template_row():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL
    ).update(subject="Custom subject: {{status}}", body="Custom body: {{status}}")

    subject, body = render_email_or_default(
        NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL,
        {"status": "Dispatched"},
        default_subject="Fallback subject",
        default_body="Fallback body",
    )

    assert subject == "Custom subject: Dispatched"
    assert body == "Custom body: Dispatched"


@pytest.mark.django_db
def test_render_email_or_default_falls_back_when_no_row_exists():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL
    ).delete()

    subject, body = render_email_or_default(
        NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL,
        {"status": "Dispatched"},
        default_subject="Order is {{status}}",
        default_body="Now {{status}}.",
    )

    assert subject == "Order is Dispatched"
    assert body == "Now Dispatched."
