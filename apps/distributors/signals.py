import logging
from decimal import Decimal

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.wallet.models import Wallet, WalletTransaction

from .realtime import wallet_group_name

logger = logging.getLogger(__name__)

_ZERO = Decimal("0.00")


@receiver(post_save, sender=WalletTransaction)
def notify_wallet_balance_changed(sender, instance, created, **kwargs):
    """Pushes a live wallet-balance update to the owning distributor's
    dashboard over Channels (Task 20d). Deliberately decoupled from
    apps.wallet.services.credit()/debit() themselves -- the wallet app has
    no notion of Channels, consumers, or WebSockets; this receiver lives
    in apps.distributors (which owns the dashboard/real-time concern) and
    listens for WalletTransaction being created instead.

    Every WalletTransaction row represents a real balance change under
    this ledger's own append-only design ("balance is always the sum of
    ledger entries" -- see Wallet/WalletTransaction docstrings), so
    post_save on this specific model is a correct "balance changed"
    signal here -- this is not a generic event-bus pattern being reused
    for something it doesn't fit.

    Known limitation, not silently ignored: Django signals do not fire on
    bulk_create(). Every current caller (apps.wallet.services.credit and
    debit) uses a single .create(), so this holds today, but a future
    bulk-write path for WalletTransaction would silently produce no live
    update for it. See the Task 20d ADR.

    created=True guard: only a new row represents a balance change under
    this append-only design; guards against a hypothetical future .save()
    on an existing row (e.g. a metadata correction) re-firing a duplicate
    notification.
    """
    if not created:
        return

    distributor_id = instance.wallet.distributor_id
    transaction.on_commit(lambda: _send_balance_update(distributor_id))


def _send_balance_update(distributor_id):
    # Queried fresh here, at actual commit time -- never a value captured
    # earlier -- so the message always reflects the true current balance
    # even if multiple credits/debits commit in close succession (a
    # fresh-context review flagged the stale/out-of-order-delivery risk of
    # doing anything else).
    try:
        balance = Wallet.objects.get(distributor_id=distributor_id).balance
    except Wallet.DoesNotExist:
        balance = _ZERO

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    try:
        async_to_sync(channel_layer.group_send)(
            wallet_group_name(distributor_id),
            {"type": "wallet_balance_update", "balance": str(balance)},
        )
    except Exception:
        # A Channels/Redis hiccup must never surface as a failure of the
        # already-committed credit/debit that triggered this -- mirrors
        # Task 12's SMS-notification-failure isolation exactly (see
        # CLAUDE.md): the money already moved correctly; a live-update
        # delivery failure is a UI staleness problem, not a data problem.
        logger.exception(
            "wallet balance live-update failed to send, distributor_id=%s",
            distributor_id,
        )
