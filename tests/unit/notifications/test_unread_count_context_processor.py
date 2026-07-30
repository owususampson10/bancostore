from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import RequestFactory

import pytest

from apps.distributors.models import Distributor
from apps.notifications.context_processors import unread_notification_count
from apps.notifications.models import Notification

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    phone = f"+233254{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_customer():
    phone = f"+233254{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!")


def _request_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.mark.django_db
def test_returns_zero_for_anonymous_user():
    result = unread_notification_count(_request_for(AnonymousUser()))
    assert result == {"unread_notification_count": 0}


@pytest.mark.django_db
def test_returns_zero_for_authenticated_non_distributor():
    customer = _make_customer()
    result = unread_notification_count(_request_for(customer))
    assert result == {"unread_notification_count": 0}


@pytest.mark.django_db
def test_returns_zero_for_distributor_group_member_with_no_distributor_row():
    """Regression guard: is_distributor() only checks group membership,
    not that a Distributor row exists (a real, already-documented gap
    elsewhere in this codebase -- see CLAUDE.md Task 15). Since this
    context processor runs on EVERY page site-wide, a naive
    request.user.distributor access would 500 the entire site for such a
    user, not just one view."""
    phone = f"+233254{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)

    result = unread_notification_count(_request_for(user))
    assert result == {"unread_notification_count": 0}


@pytest.mark.django_db
def test_counts_only_unread_notifications_for_that_distributor():
    distributor = _make_distributor()
    Notification.objects.create(
        distributor=distributor,
        event_type=Notification.EventType.KYC_DECIDED,
        message="a",
        is_read=False,
    )
    Notification.objects.create(
        distributor=distributor,
        event_type=Notification.EventType.KYC_DECIDED,
        message="b",
        is_read=True,
    )
    Notification.objects.create(
        distributor=distributor,
        event_type=Notification.EventType.KYC_DECIDED,
        message="c",
        is_read=False,
    )

    result = unread_notification_count(_request_for(distributor.user))
    assert result == {"unread_notification_count": 2}


@pytest.mark.django_db
def test_never_counts_another_distributors_notifications():
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()
    Notification.objects.create(
        distributor=distributor_a,
        event_type=Notification.EventType.KYC_DECIDED,
        message="a",
        is_read=False,
    )

    result = unread_notification_count(_request_for(distributor_b.user))
    assert result == {"unread_notification_count": 0}
