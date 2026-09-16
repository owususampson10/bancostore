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


def _verified_session(user, with_2fa=True):
    """A real session in the configured store, optionally carrying a
    verified 2FA device -- what is_admin_portal_staff requires on the
    HTTP side, and what the consumer now requires too."""
    from importlib import import_module

    from django.conf import settings

    from django_otp import DEVICE_ID_SESSION_KEY
    from django_otp.plugins.otp_totp.models import TOTPDevice

    engine = import_module(settings.SESSION_ENGINE)
    store = engine.SessionStore()
    store["_auth_user_id"] = str(user.pk)
    if with_2fa:
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
        store[DEVICE_ID_SESSION_KEY] = device.persistent_id
    store.save()
    return store


def _connect_as(user, session=None):
    """Returns whether the consumer accepted the connection."""
    result = {}
    if session is None and getattr(user, "pk", None) is not None:
        session = _verified_session(user)

    async def run():
        communicator = WebsocketCommunicator(
            AdminNotificationConsumer.as_asgi(), "/ws/admin/notifications/"
        )
        communicator.scope["user"] = user
        communicator.scope["session"] = session
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


@pytest.mark.django_db(transaction=True)
def test_a_push_stops_once_staff_access_is_revoked_mid_session():
    """CodeRabbit (PR #92), CWE-863: an admin deactivated while the socket
    is open must stop receiving customer data immediately, not whenever
    they happen to disconnect. The check must hit the database -- the
    cached scope["user"] was serialised at connect time and cannot know
    it has been revoked."""
    from apps.notifications.realtime import admin_notification_group_name

    user = User.objects.create_user(
        username="revoked-admin", password="Passw0rd!", is_staff=True
    )
    session = _verified_session(user)
    received = {}

    async def run():
        from channels.layers import get_channel_layer

        communicator = WebsocketCommunicator(
            AdminNotificationConsumer.as_asgi(), "/ws/admin/notifications/"
        )
        communicator.scope["user"] = user
        communicator.scope["session"] = session
        connected, _ = await communicator.connect()
        assert connected is True

        # Revoked AFTER the socket is already open.
        await User.objects.filter(pk=user.pk).aupdate(is_staff=False)

        await get_channel_layer().group_send(
            admin_notification_group_name(),
            {
                "type": "notification_push",
                "id": 1,
                "event_type": "new_order",
                "message": "customer name and total",
                "created_at": "2026-09-16T00:00:00+00:00",
                "unread_count": 1,
            },
        )
        # The socket is closed on revocation, so SOMETHING arrives -- a
        # close frame. What must never arrive is the payload itself.
        output = await communicator.receive_output(timeout=2)
        received["output"] = output
        await communicator.disconnect()

    async_to_sync(run)()
    assert received["output"]["type"] == "websocket.close"
    assert "customer name and total" not in str(received["output"])


@pytest.mark.django_db(transaction=True)
def test_a_badge_update_also_stops_once_staff_access_is_revoked():
    """Code review (PR #92): the same guard on unread_count_update had
    zero coverage -- reverting it alone left every consumer test passing.
    Both handlers push to a revoked user's socket, so both need proving."""
    from apps.notifications.realtime import admin_notification_group_name

    user = User.objects.create_user(
        username="revoked-admin-2", password="Passw0rd!", is_staff=True
    )
    session = _verified_session(user)
    received = {}

    async def run():
        from channels.layers import get_channel_layer

        communicator = WebsocketCommunicator(
            AdminNotificationConsumer.as_asgi(), "/ws/admin/notifications/"
        )
        communicator.scope["user"] = user
        communicator.scope["session"] = session
        connected, _ = await communicator.connect()
        assert connected is True

        await User.objects.filter(pk=user.pk).aupdate(is_active=False)

        await get_channel_layer().group_send(
            admin_notification_group_name(),
            {"type": "unread_count_update", "unread_count": 42},
        )
        received["output"] = await communicator.receive_output(timeout=2)
        await communicator.disconnect()

    async_to_sync(run)()
    assert received["output"]["type"] == "websocket.close"
    assert "42" not in str(received["output"])


@pytest.mark.django_db(transaction=True)
def test_a_staff_user_without_verified_2fa_cannot_connect():
    """CodeRabbit (PR #92), CWE-863: the HTTP gate is is_staff AND
    is_verified(), so a socket checking only is_staff made this the
    weakest entry point in the whole admin surface -- customer names,
    phone numbers and order totals readable over a channel that the
    equivalent admin PAGE would have refused."""
    user = User.objects.create_user(
        username="no-2fa-admin", password="Passw0rd!", is_staff=True
    )
    session = _verified_session(user, with_2fa=False)

    assert _connect_as(user, session=session) is False


@pytest.mark.django_db(transaction=True)
def test_an_expired_session_cannot_connect():
    """CodeRabbit (PR #92): SessionTimeoutMiddleware only runs on HTTP
    requests and AuthMiddlewareStack never re-authenticates an open
    socket, so without this an expired session kept receiving customer
    data for as long as the DB flags stayed true."""
    user = User.objects.create_user(
        username="expired-admin", password="Passw0rd!", is_staff=True
    )
    session = _verified_session(user)
    session.delete()  # what expiry looks like to the store

    assert _connect_as(user, session=session) is False


@pytest.mark.django_db(transaction=True)
def test_a_revoked_socket_closes_with_the_policy_code():
    """CodeRabbit (PR #92): the close carried no code, so Channels sent
    1000 and the browser client treated it as an ordinary disconnect and
    reconnected every 30 seconds forever. 1008 tells the client this is a
    refusal, not a blip."""
    from apps.notifications.realtime import admin_notification_group_name

    user = User.objects.create_user(
        username="policy-code-admin", password="Passw0rd!", is_staff=True
    )
    session = _verified_session(user)
    received = {}

    async def run():
        from channels.layers import get_channel_layer

        communicator = WebsocketCommunicator(
            AdminNotificationConsumer.as_asgi(), "/ws/admin/notifications/"
        )
        communicator.scope["user"] = user
        communicator.scope["session"] = session
        connected, _ = await communicator.connect()
        assert connected is True

        await User.objects.filter(pk=user.pk).aupdate(is_staff=False)
        await get_channel_layer().group_send(
            admin_notification_group_name(),
            {"type": "unread_count_update", "unread_count": 42},
        )
        received["output"] = await communicator.receive_output(timeout=2)
        await communicator.disconnect()

    async_to_sync(run)()
    assert received["output"]["type"] == "websocket.close"
    assert received["output"].get("code") == 1008
