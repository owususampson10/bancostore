"""Task 61c. Live updates for the admin portal's notification bell.

The third real Channels consumer in this codebase, and it copies
WalletBalanceConsumer's and NotificationConsumer's connect()/auth shape
deliberately rather than inventing a third one: group membership is
derived only from the authenticated session (self.scope["user"]), never
from anything the client sends, and an unauthorised connection is
rejected at connect() rather than accepted and then silently starved of
messages.

The staff check matters more here than in the distributor consumers.
These notifications name real customers, their phone numbers and their
order totals -- a non-staff account reaching this group would be a real
data leak, not merely a wrong-inbox annoyance.

Same known limitation as the other two, stated rather than rediscovered:
a session invalidated after this socket is already open does not close
the existing connection.
"""

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.notifications.realtime import admin_notification_group_name


class AdminNotificationConsumer(AsyncWebsocketConsumer):
    group_name = None

    async def connect(self):
        if not await self._is_staff(self.scope.get("user")):
            await self.close()
            return

        self.group_name = admin_notification_group_name()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group_name is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def _still_authorised(self) -> bool:
        """CodeRabbit (PR #92), CWE-863: re-check against the DATABASE
        before every push, not against the cached scope["user"].

        An admin who is deactivated or has is_staff revoked while this
        socket is open would otherwise keep receiving customer names,
        phone numbers and order totals until they happened to disconnect.
        The two distributor consumers carry the same documented
        limitation, but it matters more here: revoking an admin's access
        is precisely the moment you need it to take effect, and this
        channel carries other people's personal data rather than the
        connected user's own.
        """
        user = self.scope.get("user")
        user_id = getattr(user, "pk", None)
        if user_id is None:
            return False
        if not await self._is_staff_by_id(user_id):
            await self.close()
            return False
        return True

    async def notification_push(self, event):
        if not await self._still_authorised():
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
        if not await self._still_authorised():
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "unread_count_update",
                    "unread_count": event["unread_count"],
                }
            )
        )

    @database_sync_to_async
    def _is_staff_by_id(self, user_id) -> bool:
        """Deliberately a fresh query by id, never a re-read of the
        cached user object -- that object was serialised at connect time
        and cannot know it has since been revoked."""
        from django.contrib.auth import get_user_model

        return (
            get_user_model()
            .objects.filter(pk=user_id, is_active=True, is_staff=True)
            .exists()
        )

    @database_sync_to_async
    def _is_staff(self, user):
        # is_active is checked explicitly: a deactivated staff account
        # whose session is still valid must not keep receiving customer
        # data. AnonymousUser has is_authenticated False, so the first
        # clause covers it without a separate branch.
        return bool(
            user is not None
            and getattr(user, "is_authenticated", False)
            and user.is_active
            and user.is_staff
        )
