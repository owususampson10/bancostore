"""Task 61c. The admin bell's WebSocket auth.

Group membership must come only from the authenticated session, never a
client-supplied value -- the shape WalletBalanceConsumer and
NotificationConsumer already established. A non-staff user reaching the
admin bell would be a real data leak, not a wrong-inbox annoyance: these
notifications name real customers, their phone numbers and order totals.

Uses async_to_sync around an inner run(), matching
tests/feature/distributors/test_notification_consumer.py exactly rather
than adding pytest-asyncio -- a new dependency needs its own decision,
and this codebase already has a working convention for the same problem.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator

from apps.admin_portal.consumers import AdminNotificationConsumer

User = get_user_model()


def _connect_as(user):
    """Returns whether the consumer accepted the connection."""
    result = {}

    async def run():
        communicator = WebsocketCommunicator(
            AdminNotificationConsumer.as_asgi(), "/ws/admin/notifications/"
        )
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        result["connected"] = connected
        await communicator.disconnect()

    async_to_sync(run)()
    return result["connected"]


@pytest.mark.django_db(transaction=True)
def test_a_staff_user_may_connect():
    user = User.objects.create_user(
        username="admin-bell", password="Passw0rd!", is_staff=True
    )

    assert _connect_as(user) is True


@pytest.mark.django_db(transaction=True)
def test_a_non_staff_user_is_rejected():
    """Rejected at connect(), not accepted-then-silently-ignored -- the
    convention the two existing consumers set, so a client gets a clear
    failure rather than an open socket that never delivers."""
    user = User.objects.create_user(
        username="not-admin", password="Passw0rd!", is_staff=False
    )

    assert _connect_as(user) is False


@pytest.mark.django_db(transaction=True)
def test_an_anonymous_visitor_is_rejected():
    assert _connect_as(AnonymousUser()) is False


@pytest.mark.django_db(transaction=True)
def test_a_deactivated_staff_account_is_rejected():
    """A staff account that has been switched off must stop receiving
    customer data even if its session is still technically valid.
    is_staff alone would let it straight back in."""
    user = User.objects.create_user(
        username="ex-admin", password="Passw0rd!", is_staff=True
    )
    user.is_active = False
    user.save(update_fields=["is_active"])

    assert _connect_as(user) is False
