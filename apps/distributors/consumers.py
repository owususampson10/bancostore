import json
from decimal import Decimal

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.accounts.permissions import is_distributor
from apps.distributors.models import Distributor
from apps.wallet.models import Wallet

from .realtime import notification_group_name, wallet_group_name

_ZERO = Decimal("0.00")


class WalletBalanceConsumer(AsyncWebsocketConsumer):
    """Live wallet-balance updates for a distributor's own dashboard
    (Task 20d) -- the first real Channels consumer in this codebase.

    Group membership is derived only from the authenticated session
    (self.scope["user"], populated by AuthMiddlewareStack from the Django
    session cookie in bancostore/asgi.py) -- never from a client-supplied
    ID, so there is no input to validate or spoof for group-joining
    purposes. Rejects at connect() rather than accepting then silently
    withholding messages, matching apps.accounts.permissions.is_distributor
    itself for the distributor-role check, exactly as regular HTTP views
    in this codebase do.

    Known limitation, not silently ignored: a session invalidated after
    this socket connects (logout in another tab, admin deactivation) does
    not close an already-open connection -- Channels' auth check runs once,
    at the handshake, not per message. Deferred; see the Task 20d ADR.
    """

    group_name = None

    async def connect(self):
        distributor = await self._get_distributor(self.scope["user"])
        if distributor is None:
            await self.close()
            return

        self.group_name = wallet_group_name(distributor.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send the current balance immediately on connect, not just on the
        # next change -- otherwise a reconnect (network blip, page refresh)
        # leaves a stale balance on screen until the next unrelated wallet
        # event happens to fire (a fresh-context review caught this gap).
        balance = await self._get_current_balance(distributor.pk)
        await self.send(text_data=self._encode(balance))

    async def disconnect(self, close_code):
        if self.group_name is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def wallet_balance_update(self, event):
        await self.send(text_data=self._encode(event["balance"]))

    @staticmethod
    def _encode(balance) -> str:
        return json.dumps({"type": "wallet_balance_update", "balance": str(balance)})

    @database_sync_to_async
    def _get_distributor(self, user):
        if not user.is_authenticated or not is_distributor(user):
            return None
        try:
            return user.distributor
        except Distributor.DoesNotExist:
            return None

    @database_sync_to_async
    def _get_current_balance(self, distributor_id) -> Decimal:
        try:
            return Wallet.objects.get(distributor_id=distributor_id).balance
        except Wallet.DoesNotExist:
            return _ZERO


class NotificationConsumer(AsyncWebsocketConsumer):
    """Live notification-bell updates for a distributor's own dashboard
    (Task 21d) -- the second real Channels consumer in this codebase,
    mirroring WalletBalanceConsumer's connect()/auth shape exactly: group
    membership derived only from the authenticated session
    (self.scope["user"]), never a client-supplied ID, and rejects at
    connect() rather than accepting then silently withholding messages.

    No initial push on connect (unlike WalletBalanceConsumer's single
    scalar balance) -- the bell's initial notification list/unread count
    is a bulk fetch, better served by a normal HTTP view than a
    WebSocket push; this consumer only forwards live, incremental
    updates. Same known limitation as WalletBalanceConsumer, not repeated
    in full here: a session invalidated after this socket connects does
    not close an already-open connection.
    """

    group_name = None

    async def connect(self):
        distributor = await self._get_distributor(self.scope["user"])
        if distributor is None:
            await self.close()
            return

        self.group_name = notification_group_name(distributor.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group_name is not None:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def notification_push(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "notification_push",
                    "id": event["id"],
                    "event_type": event["event_type"],
                    "message": event["message"],
                    "created_at": event["created_at"],
                }
            )
        )

    @database_sync_to_async
    def _get_distributor(self, user):
        if not user.is_authenticated or not is_distributor(user):
            return None
        try:
            return user.distributor
        except Distributor.DoesNotExist:
            return None
