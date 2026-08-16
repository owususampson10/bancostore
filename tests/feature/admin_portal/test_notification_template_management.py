from django.urls import reverse

import pytest

from apps.notifications.models import NotificationTemplate


def _list_url():
    return reverse("admin_portal:notification_template_list")


def _edit_url(template):
    return reverse("admin_portal:notification_template_edit", args=[template.pk])


def _make_template(key=NotificationTemplate.Key.OTP_CODE, **overrides):
    """Every Key is pre-seeded by migration 0006_seed_notification_templates
    -- fetches that already-existing row and applies overrides, rather
    than creating a second row that would collide with `key`'s unique
    constraint."""
    template = NotificationTemplate.objects.get(key=key)
    for field, value in overrides.items():
        setattr(template, field, value)
    if overrides:
        template.save()
    return template


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_template_list(client, db):
    response = client.get(_list_url())
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_template_list_shows_seeded_rows(staff_client):
    _make_template()

    response = staff_client.get(_list_url())

    assert response.status_code == 200
    assert b"OTP Verification Code" in response.content


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_edit_a_templates_wording(staff_client):
    template = _make_template()

    response = staff_client.post(
        _edit_url(template),
        {"subject": "", "body": "New wording: {{code}} expires soon."},
    )

    assert response.status_code == 302
    template.refresh_from_db()
    assert template.body == "New wording: {{code}} expires soon."


@pytest.mark.django_db
def test_editing_with_an_undeclared_placeholder_is_rejected(staff_client):
    template = _make_template()

    response = staff_client.post(
        _edit_url(template),
        {"subject": "", "body": "Your code is {{secret_internal_field}}."},
    )

    assert response.status_code == 200
    template.refresh_from_db()
    assert "secret_internal_field" not in template.body
    assert b"Unknown placeholder" in response.content


@pytest.mark.django_db
def test_editing_with_a_declared_placeholder_is_accepted(staff_client):
    template = _make_template(
        key=NotificationTemplate.Key.WITHDRAWAL_REJECTED,
        body="Your withdrawal of GHS {{amount}} was rejected: {{reason}}",
    )

    response = staff_client.post(
        _edit_url(template),
        {"subject": "", "body": "Rejected -- GHS {{amount}}: {{reason}}"},
    )

    assert response.status_code == 302
    template.refresh_from_db()
    assert template.body == "Rejected -- GHS {{amount}}: {{reason}}"


@pytest.mark.django_db
def test_a_subject_containing_a_newline_is_rejected(staff_client):
    template = _make_template()

    response = staff_client.post(
        _edit_url(template),
        {"subject": "Hello\nBcc: attacker@example.test", "body": "{{code}}"},
    )

    assert response.status_code == 200
    template.refresh_from_db()
    assert template.subject == ""
    assert b"line breaks" in response.content


@pytest.mark.django_db
def test_removing_code_from_an_otp_template_is_rejected(staff_client):
    """CodeRabbit finding (PR #79): PLACEHOLDERS_BY_KEY only says which
    placeholders are ALLOWED -- nothing stopped an admin from deleting
    {{code}} from an OTP body entirely, silently breaking every real OTP
    send afterward (the SMS would go out with no code in it at all)."""
    template = _make_template()

    response = staff_client.post(
        _edit_url(template),
        {"subject": "", "body": "Your verification message has arrived."},
    )

    assert response.status_code == 200
    template.refresh_from_db()
    assert "{{code}}" in template.body  # rejected -- the seeded body is untouched
    assert b"must include" in response.content


@pytest.mark.django_db
def test_a_subject_on_a_non_email_template_is_rejected(staff_client):
    """CodeRabbit finding (PR #79): only ORDER_STATUS_UPDATE_EMAIL's send
    site ever reads `subject` -- a non-empty subject saved against an
    SMS/in-app key looks like a successful edit but silently does
    nothing on the next real send."""
    template = _make_template()

    response = staff_client.post(
        _edit_url(template),
        {"subject": "This subject is never read", "body": "{{code}}"},
    )

    assert response.status_code == 200
    template.refresh_from_db()
    assert template.subject == ""
    assert b"only available for email" in response.content


@pytest.mark.django_db
def test_a_subject_on_the_order_status_email_template_is_accepted(staff_client):
    template = _make_template(
        key=NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL,
        subject="Old subject",
        body="Old body {{status}}",
    )

    response = staff_client.post(
        _edit_url(template),
        {
            "subject": "Your order is {{status}}",
            "body": "Order {{reference}}: {{status}}",
        },
    )

    assert response.status_code == 302
    template.refresh_from_db()
    assert template.subject == "Your order is {{status}}"


@pytest.mark.django_db
def test_editing_records_history_for_the_audit_log(staff_client):
    """Task 48a's own security-review finding: this feeds the same
    Task 47e unified audit log as Category/Product/PlatformSettingChange."""
    template = _make_template()
    history_count_before = template.history.count()

    staff_client.post(_edit_url(template), {"subject": "", "body": "Updated: {{code}}"})

    assert template.history.count() == history_count_before + 1
    latest = template.history.first()
    assert latest.body == "Updated: {{code}}"
