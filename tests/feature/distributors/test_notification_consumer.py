from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.distributors.consumers import NotificationConsumer
from apps.distributors.models import Distributor
from apps.notifications.models import Notification
from apps.notifications.services import send_notification

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    """Mirrors test_dashboard_live_updates.py's own helper."""
    phone = f"+233253{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_customer(**overrides):
    phone = f"+233253{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!", **overrides)


async def _connect_as(user):
    communicator = WebsocketCommunicator(
        NotificationConsumer.as_asgi(), "/ws/distributors/notifications/"
    )
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    return communicator, connected


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_connection_is_rejected():
    async def run():
        communicator, connected = await _connect_as(AnonymousUser())
        assert connected is False
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_non_distributor_authenticated_user_is_rejected():
    customer = _make_customer()

    async def run():
        communicator, connected = await _connect_as(customer)
        assert connected is False
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_distributor_connection_is_accepted():
    distributor = _make_distributor()

    async def run():
        communicator, connected = await _connect_as(distributor.user)
        assert connected is True
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_a_notification_sent_after_connecting_pushes_live_to_that_client():
    distributor = _make_distributor()

    async def run():
        communicator, connected = await _connect_as(distributor.user)
        assert connected is True

        await database_sync_to_async(send_notification)(
            distributor, Notification.EventType.KYC_DECIDED, "Your KYC was approved"
        )

        response = await communicator.receive_json_from()
        assert response["type"] == "notification_push"
        assert response["event_type"] == "kyc_decided"
        assert response["message"] == "Your KYC was approved"
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_a_distributor_never_receives_another_distributors_notification():
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()

    async def run():
        communicator_a, connected_a = await _connect_as(distributor_a.user)
        assert connected_a is True
        communicator_b, connected_b = await _connect_as(distributor_b.user)
        assert connected_b is True

        await database_sync_to_async(send_notification)(
            distributor_a, Notification.EventType.KYC_DECIDED, "for A only"
        )

        response_a = await communicator_a.receive_json_from()
        assert response_a["message"] == "for A only"

        assert await communicator_b.receive_nothing(timeout=0.5) is True

        await communicator_a.disconnect()
        await communicator_b.disconnect()

    async_to_sync(run)()
