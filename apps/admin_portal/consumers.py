"""Task 61c. Live updates for the admin portal's notification bell.

The third real Channels consumer in this codebase. It copies
WalletBalanceConsumer's and NotificationConsumer's connect()/auth shape
deliberately rather than inventing a third one: group membership is
derived only from the authenticated session, never from anything the
client sends, and an unauthorised connection is rejected at connect()
rather than accepted and then silently starved of messages.

Authorization here is STRICTER than in those two, because this channel
carries other people's personal data -- customer names, phone numbers and
order totals -- rather than the connected user's own. Three things are
checked, and all three are re-checked before every push:

1. is_staff and is_active, read fresh from the DATABASE by id. The
   scope["user"] was serialised at connect time and cannot know it has
   since been revoked.
2. A verified 2FA session. `is_admin_portal_staff` (the HTTP gate) is
   `is_staff AND is_verified()`, so without this the socket would be the
   weakest entry point in the whole admin surface -- a staff account that
   logged in but never completed 2FA could read customer data here that
   it cannot read on any admin page.
3. The session is still valid and still belongs to this user.
   SessionTimeoutMiddleware only runs on HTTP requests, and
   AuthMiddlewareStack never re-authenticates an already open socket, so
   an expired session would otherwise keep receiving pushes indefinitely.
4. The session's auth hash still matches the user's current one -- so a
   password change, the standard way to evict a stolen session, closes
   the socket too instead of only bouncing HTTP requests.

All four were raised across review rounds on PR #92; the original version
checked only is_staff, at connect time only.
"""

import json
from importlib import import_module

from django.conf import settings

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django_otp import DEVICE_ID_SESSION_KEY

from apps.notifications.realtime import admin_notification_group_name

# RFC 6455 policy-violation close code. Used ONLY for revocation of an
# already-accepted socket, so the browser client can tell "you may no
# longer have this data" apart from an ordinary disconnect and stop
# reconnecting. A close during connect() happens before the handshake
# completes and cannot carry a policy code to the browser, which is why
# that path does not use it.
REVOKED_CLOSE_CODE = 1008


class AdminNotificationConsumer(AsyncWebsocketConsumer):
    group_name = None

    async def connect(self):
        if not await self._is_authorised():
            await self.close()
            return

        self.group_name = admin_notification_group_name()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group_name is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def notification_push(self, event):
        if not await self._revoke_if_unauthorised():
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "notification_push",
                    "id": event["id"],
                    "event_type": event["event_type"],
                    "message": event["message"],
                    "created_at": event["created_at"],
                    "unread_count": event["unread_count"],
                }
            )
        )

    async def unread_count_update(self, event):
        """Cross-tab badge reconciliation, mirroring NotificationConsumer:
        one admin marking a notification read in one tab must not leave a
        stale badge in another."""
        if not await self._revoke_if_unauthorised():
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "unread_count_update",
                    "unread_count": event["unread_count"],
                }
            )
        )

    async def _revoke_if_unauthorised(self) -> bool:
        """True when the push may proceed. Otherwise leaves the group and
        closes with the policy code.

        group_discard runs BEFORE close: the close only takes effect once
        the client completes the handshake, so without this every queued
        event would cost another DB round-trip and another close frame in
        the meantime.
        """
        if await self._is_authorised():
            return True
        if self.group_name is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            self.group_name = None
        await self.close(code=REVOKED_CLOSE_CODE)
        return False

    async def _is_authorised(self) -> bool:
        user = self.scope.get("user")
        user_id = getattr(user, "pk", None)
        if user_id is None or not getattr(user, "is_authenticated", False):
            return False
        session = self.scope.get("session")
        session_key = getattr(session, "session_key", None)
        if not session_key:
            return False
        return await self._check(user_id, session_key)

    @database_sync_to_async
    def _check(self, user_id, session_key) -> bool:
        """One DB round-trip's worth of checks, all against live state.

        Cost note: the group is `admin_notifications`, sized by open admin
        TABS rather than by user count, so this is a handful of queries
        per order even at this project's stated scale -- not a per-user
        cost. Deliberately not cached: the point of the check is that
        revocation takes effect immediately.
        """
        from django.contrib.auth import HASH_SESSION_KEY, get_user_model

        from django_otp.models import Device

        user = (
            get_user_model()
            .objects.filter(pk=user_id, is_active=True, is_staff=True)
            .first()
        )
        if user is None:
            return False

        # load(), not exists(). Code review (PR #92) showed exists() is not
        # a guard at all under this project's cached_db engine: DBStore
        # .exists does not filter on expire_date, so an expired row that
        # clearsessions has not yet removed still reads as existing.
        # load() DOES filter on it and returns {} for an expired session,
        # which the _auth_user_id comparison below then rejects.
        engine = import_module(settings.SESSION_ENGINE)
        data = engine.SessionStore(session_key).load()
        if str(data.get("_auth_user_id") or "") != str(user_id):
            return False

        # Code review (PR #92), proven live: a password change did not
        # close an open socket. Django's HTTP get_user compares this
        # stored hash with the user's current one on every request;
        # Channels does it once, at connect. Changing a password is the
        # standard way to evict a stolen session, and the session row
        # still exists afterwards, so nothing else here would notice.
        # SECRET_KEY_FALLBACKS mirrors Django 5.2's own get_user, so
        # rotating the secret key does not lock every admin out.
        if not self._session_hash_matches(user, data.get(HASH_SESSION_KEY)):
            return False

        # The 2FA half of is_admin_portal_staff. Verified against the
        # installed django-otp 1.7.0: OTPMiddleware resolves exactly this
        # key with from_persistent_id and requires device.user_id to match
        # -- it does not check `confirmed`, so neither does this, keeping
        # the socket equal to the HTTP gate rather than stricter or looser.
        device_id = data.get(DEVICE_ID_SESSION_KEY)
        if not device_id:
            return False
        device = Device.from_persistent_id(device_id)
        return device is not None and device.user_id == user_id

    @staticmethod
    def _session_hash_matches(user, session_hash) -> bool:
        from django.utils.crypto import constant_time_compare

        if not session_hash:
            return False
        if constant_time_compare(session_hash, user.get_session_auth_hash()):
            return True
        return any(
            constant_time_compare(
                session_hash, user._get_session_auth_hash(secret=fallback)
            )
            for fallback in getattr(settings, "SECRET_KEY_FALLBACKS", [])
        )
