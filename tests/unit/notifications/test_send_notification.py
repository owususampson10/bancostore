from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

import pytest

from apps.distributors.models import Distributor
from apps.notifications.models import Notification
from apps.notifications.services import push_unread_count_update, send_notification

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233252{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db(transaction=True)
def test_send_notification_persists_a_notification_row():
    distributor = _make_distributor()

    send_notification(
        distributor, Notification.EventType.KYC_DECIDED, "Your KYC was approved"
    )

    notification = Notification.objects.get(distributor=distributor)
    assert notification.event_type == Notification.EventType.KYC_DECIDED
    assert notification.message == "Your KYC was approved"
    assert notification.is_read is False


@pytest.mark.django_db(transaction=True)
def test_send_notification_does_not_persist_or_push_on_caller_rollback(monkeypatch):
    """Task 21d-i's core doubt-driven-development fix: send_notification
    must be deferred via transaction.on_commit() so a caller's rollback
    (an already-in-progress business transaction failing for an unrelated
    reason) means the notification never happened at all -- not a
    dangling row, and definitely not a live push for something that got
    rolled back."""
    distributor = _make_distributor()
    push_calls = []
    monkeypatch.setattr(
        "apps.notifications.services._push_live",
        lambda *args, **kwargs: push_calls.append((args, kwargs)),
    )

    class _DeliberateRollback(Exception):
        pass

    with pytest.raises(_DeliberateRollback):
        with transaction.atomic():
            send_notification(distributor, Notification.EventType.KYC_DECIDED, "test")
            raise _DeliberateRollback()

    assert Notification.objects.count() == 0
    assert push_calls == []


@pytest.mark.django_db(transaction=True)
def test_a_notification_create_failure_never_propagates_to_the_caller(monkeypatch):
    def _raise(**kwargs):
        raise IntegrityError("forced failure for this test")

    monkeypatch.setattr(
        "apps.notifications.services.Notification.objects.create", _raise
    )
    distributor = _make_distributor()

    # Must not raise -- a persistence failure is logged, never surfaced.
    send_notification(distributor, Notification.EventType.KYC_DECIDED, "test")


@pytest.mark.django_db(transaction=True)
def test_a_live_push_failure_never_propagates_and_the_row_still_persists(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("forced Channels/Redis failure for this test")

    monkeypatch.setattr("apps.notifications.services._push_live", _raise)
    distributor = _make_distributor()

    # Must not raise, and the already-created row must survive regardless
    # of whether the live push succeeds.
    send_notification(distributor, Notification.EventType.KYC_DECIDED, "test")

    assert Notification.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db(transaction=True)
def test_an_over_length_message_is_truncated_instead_of_failing_to_persist():
    """CodeRabbit finding (PR #79): Notification.message is
    max_length=500, but a rendered NotificationTemplate body has no
    length bound of its own -- an admin-edited template, or a long
    substituted value, could exceed it. This runs after the caller's own
    transaction has already committed and is wrapped in try/except, so
    an over-length value could never roll back the business event that
    triggered it -- but without truncation it would silently drop the
    notification entirely once the DB write failed."""
    distributor = _make_distributor()
    max_length = Notification._meta.get_field("message").max_length
    too_long = "x" * (max_length + 100)

    send_notification(distributor, Notification.EventType.KYC_DECIDED, too_long)

    notification = Notification.objects.get(distributor=distributor)
    assert len(notification.message) == max_length
    assert notification.message == too_long[:max_length]


@pytest.mark.django_db(transaction=True)
def test_a_distributor_never_sees_another_distributors_notification_row():
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()

    send_notification(distributor_a, Notification.EventType.KYC_DECIDED, "for A only")

    assert Notification.objects.filter(distributor=distributor_b).count() == 0


@pytest.mark.django_db(transaction=True)
def test_push_unread_count_update_never_raises_on_a_channels_failure(monkeypatch):
    """Mirrors send_notification's own failure-isolation convention
    exactly (test_a_live_push_failure_never_propagates...): a Channels/
    Redis hiccup while broadcasting the cross-tab unread-count
    reconciliation must never surface to the caller -- it's a UI
    staleness problem, not a reason to fail the mark-read/mark-all-read
    request that triggered it."""

    def _raise(*args, **kwargs):
        raise RuntimeError("forced Channels/Redis failure for this test")

    monkeypatch.setattr("apps.notifications.services.get_channel_layer", _raise)
    distributor = _make_distributor()

    # Must not raise.
    push_unread_count_update(distributor)
