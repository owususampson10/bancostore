"""Task 67. A payment Paystack confirmed that did not become what it paid for
leaves a durable PaymentIssue, and the admin hears about it once.

Found 2026-09-16: a distributor paid the registration fee on a checkout page
left open past the pending registration's cleanup. No account was created,
and the only trace was a line in the server log.
"""

from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.core import mail
from django.db import transaction

import pytest
from constance import config

from apps.distributors.models import PaymentIssue, PendingRegistration
from apps.distributors.payment_issues import record_payment_issue
from apps.notifications.models import AdminNotification
from tests.unit.distributors.test_consume_paid_registration import _make_sponsor

KIND = PaymentIssue.Kind.REGISTRATION_UNMATCHED


@pytest.fixture
def locmem(settings, monkeypatch):
    """Also runs on_commit callbacks straight away: the alert is deferred to
    commit, which a plain django_db test (one rolled-back transaction) never
    reaches. The deferral itself is covered by the transaction=True test at
    the bottom of this file."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


def _verified(**overrides):
    data = {
        "status": "success",
        "amount": 10000,
        "currency": "GHS",
        "paid_at": "2026-09-16T09:39:01.000Z",
        "customer": {"email": "payer@example.test"},
        "metadata": {"full_name": "Ama Owusu", "phone_number": "+233241234567"},
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_records_the_issue_with_the_payer_from_paystack_metadata(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    issue = record_payment_issue("reg-abc", KIND, verified=_verified())

    issue.refresh_from_db()
    assert issue.kind == KIND
    assert issue.amount_pesewas == 10000
    assert issue.paid_at is not None
    assert issue.payer_name == "Ama Owusu"
    assert issue.payer_phone == "+233241234567"
    assert issue.payer_email == "payer@example.test"


@pytest.mark.django_db
def test_emails_the_admin_and_rings_the_bell(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    record_payment_issue("reg-abc", KIND, verified=_verified())

    (message,) = mail.outbox
    assert message.to == ["ops@bancostore.test"]
    assert "reg-abc" in message.body
    assert "Ama Owusu" in message.body
    assert "100.00" in message.body
    bell = AdminNotification.objects.get()
    assert bell.event_type == AdminNotification.EventType.PAYMENT_ISSUE
    assert "reg-abc" in bell.message


@pytest.mark.django_db
def test_blank_setting_falls_back_to_the_order_alert_email(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_EMAIL = "orders@bancostore.test"

    record_payment_issue("reg-abc", KIND, verified=_verified())

    (message,) = mail.outbox
    assert message.to == ["orders@bancostore.test"]


@pytest.mark.django_db
def test_no_address_anywhere_still_records_and_rings_the_bell(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_EMAIL = "  "

    record_payment_issue("reg-abc", KIND, verified=_verified())

    assert mail.outbox == []
    assert PaymentIssue.objects.filter(reference="reg-abc").exists()
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_the_same_problem_on_one_reference_alerts_only_once(locmem):
    """A DIFFERENT problem on the same payment does alert again -- see
    test_a_dispute_on_an_already_recorded_payment_is_still_heard."""
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    first = record_payment_issue("reg-abc", KIND, verified=_verified())
    second = record_payment_issue("reg-abc", KIND, verified=_verified())

    assert first.pk == second.pk
    assert second.kind == KIND
    assert len(mail.outbox) == 1
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_payer_details_come_from_the_pending_registration_when_given(locmem):
    pending = PendingRegistration.objects.create(
        full_name="Kofi Mensah",
        phone_number="+233241111111",
        email="kofi@example.test",
        address="1 Road",
        area="Osu",
        password_hash=make_password("x"),
        sponsor=_make_sponsor(),
    )

    issue = record_payment_issue(
        "reg-abc", KIND, verified=_verified(metadata=""), pending=pending
    )

    assert issue.payer_name == "Kofi Mensah"
    assert issue.payer_phone == "+233241111111"


@pytest.mark.django_db
def test_without_metadata_the_phone_comes_from_the_placeholder_email(locmem):
    verified = _verified(
        metadata=None, customer={"email": "guest-233241234567@guests.bancostore.com"}
    )

    issue = record_payment_issue("reg-abc", KIND, verified=verified)

    assert issue.payer_phone == "+233241234567"


@pytest.mark.django_db
def test_hostile_metadata_is_cleaned_and_capped(locmem):
    """Paystack's inline checkout takes a public key, so metadata is text a
    stranger can choose."""
    verified = _verified(
        metadata={"full_name": "Evil\r\nBcc: x@y.z" + "A" * 500, "phone_number": 7}
    )

    issue = record_payment_issue("reg-abc", KIND, verified=verified)

    assert "\n" not in issue.payer_name and "\r" not in issue.payer_name
    assert len(issue.payer_name) <= 255
    assert issue.payer_phone == "7"


@pytest.mark.django_db
def test_metadata_sent_as_a_json_string_is_still_read(locmem):
    verified = _verified(metadata='{"full_name": "Ama Owusu"}')

    issue = record_payment_issue("reg-abc", KIND, verified=verified)

    assert issue.payer_name == "Ama Owusu"


@pytest.mark.django_db
def test_a_failing_email_never_raises_and_the_issue_stays(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    with patch(
        "apps.distributors.payment_issues.send_mail", side_effect=OSError("smtp")
    ):
        issue = record_payment_issue("reg-abc", KIND, verified=_verified())

    assert PaymentIssue.objects.filter(pk=issue.pk).exists()
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_a_database_failure_never_raises(locmem):
    with patch.object(
        PaymentIssue.objects, "get_or_create", side_effect=RuntimeError("db down")
    ):
        assert record_payment_issue("reg-abc", KIND, verified=_verified()) is None


@pytest.mark.django_db(transaction=True)
def test_the_alert_waits_for_the_surrounding_transaction_to_commit(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    with transaction.atomic():
        record_payment_issue("reg-abc", KIND, verified=_verified())
        assert mail.outbox == []

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_an_unconfirmed_registration_email_does_not_say_it_was_paid(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    record_payment_issue("reg-abc", PaymentIssue.Kind.REGISTRATION_UNCONFIRMED)

    (message,) = mail.outbox
    assert "Paystack confirmed" not in message.body
    assert "could not be reached" in message.body


@pytest.mark.django_db
def test_a_paid_issue_email_tells_the_admin_to_refund(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    record_payment_issue("reg-abc", KIND, verified=_verified())

    (message,) = mail.outbox
    assert "Paystack confirmed this payment" in message.body
    assert "Refund" in message.body


# --- How a payment is labelled on the admin screen (Task 67c) ------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "reference,expected",
    [
        ("reg-a7dc13a96d844de081e5ba21dc5cadfc-80151a91", "Registration fee"),
        ("pack-12-9f2c1d44", "Starter pack"),
        ("order-25afaf10775745439dea", "Order"),
        ("T123456789", "Payment"),
    ],
)
def test_the_payment_type_is_spelled_out_never_left_as_a_prefix(reference, expected):
    """The admin should not have to know that "reg-" means a registration
    fee."""
    assert PaymentIssue(reference=reference).payment_type_label == expected


@pytest.mark.django_db
def test_a_long_reference_is_shortened_in_the_middle():
    issue = PaymentIssue(reference="reg-a7dc13a96d844de081e5ba21dc5cadfc-80151a91")

    short = issue.short_reference

    assert short.startswith("reg-a7dc13a9")
    assert short.endswith("80151a91")
    assert "…" in short
    assert len(short) < len(issue.reference)


@pytest.mark.django_db
def test_a_short_reference_is_left_alone():
    assert PaymentIssue(reference="pack-12-9f2c1d44").short_reference == (
        "pack-12-9f2c1d44"
    )


# --- Task 68j: the platform says so if nobody can be alerted ------------------


@pytest.mark.django_db
def test_a_warning_when_no_alert_email_is_set_anywhere():
    from apps.distributors.checks import (
        PAYMENT_ALERT_EMAIL_WARNING_ID,
        payment_alert_email_is_set,
    )

    config.PAYMENT_ISSUE_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_EMAIL = "  "

    (warning,) = payment_alert_email_is_set(None)

    assert warning.id == PAYMENT_ALERT_EMAIL_WARNING_ID
    assert "admin bell" in warning.msg


@pytest.mark.django_db
def test_no_warning_once_either_address_is_set():
    from apps.distributors.checks import payment_alert_email_is_set

    config.PAYMENT_ISSUE_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"

    assert payment_alert_email_is_set(None) == []


@pytest.mark.django_db
def test_an_unreachable_settings_store_does_not_warn_misleadingly():
    from apps.distributors.checks import payment_alert_email_is_set

    class Unreachable:
        def __getattr__(self, name):
            raise RuntimeError("redis down")

    # The check does `from constance import config` at call time, so
    # replacing the module attribute is what a real outage looks like to it.
    with patch("constance.config", Unreachable()):
        assert payment_alert_email_is_set(None) == []


# --- Agent code review: a different problem on the same payment --------------


@pytest.mark.django_db
def test_a_dispute_on_an_already_recorded_payment_is_still_heard(locmem):
    """One row per payment is right for "refund this once", but a dispute
    raised on a payment already recorded has a bank deadline and is lost by
    default if nobody answers it."""
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"
    record_payment_issue(
        "order-abc",
        PaymentIssue.Kind.REFUND_RECEIVED,
        verified=_verified(),
        detail="Refunded automatically.",
        resolved=True,
    )

    record_payment_issue(
        "order-abc",
        PaymentIssue.Kind.DISPUTE_OPENED,
        detail="A customer has disputed this payment.",
    )

    issue = PaymentIssue.objects.get(reference="order-abc")
    assert issue.kind == PaymentIssue.Kind.DISPUTE_OPENED
    assert issue.resolved_at is None  # back on the to-do list
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_the_same_problem_twice_still_alerts_only_once(locmem):
    config.PAYMENT_ISSUE_ALERT_EMAIL = "ops@bancostore.test"

    record_payment_issue("order-abc", KIND, verified=_verified())
    record_payment_issue("order-abc", KIND, verified=_verified())

    assert len(mail.outbox) == 1
    assert PaymentIssue.objects.count() == 1
