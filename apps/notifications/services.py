import logging

from django.db import transaction

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.distributors.realtime import notification_group_name

from .models import Notification

logger = logging.getLogger(__name__)


def send_notification(distributor, event_type, message):
    """Creates a Notification row and pushes it live over Channels (Task
    21d) for exactly the 6 event types Section 6.6 names -- called
    explicitly at each trigger site (not via a generic Django signal),
    since unlike Task 20d's WalletBalanceConsumer (which listens for
    WalletTransaction being created -- every such row IS a balance
    change), these 6 events are heterogeneous and share no single
    underlying model whose post_save would fire for exactly these cases
    and no others. Matches this codebase's existing SMS-notification
    convention of an explicit call at each event site.

    Deferred via transaction.on_commit() unconditionally -- safe to call
    even outside an active transaction, since Django runs on_commit
    callables immediately in that case -- so this can never run before
    the caller's own transaction (a bonus credit, a placement, a
    withdrawal approval, all already-locked blocks in this codebase)
    actually commits. A doubt-driven-development review before this was
    written caught the original draft doing the DB write and the
    Channels push synchronously, unguarded, directly inside the caller's
    own lock -- risking both a rolled-back business event (if the write
    raised) and a live push for a notification that then never actually
    persisted (if the caller's transaction later rolled back). Fixed by
    this deferred, individually-try/excepted shape.
    """
    transaction.on_commit(lambda: _create_and_push(distributor, event_type, message))


def _create_and_push(distributor, event_type, message):
    try:
        notification = Notification.objects.create(
            distributor=distributor, event_type=event_type, message=message
        )
    except Exception:
        logger.exception(
            "send_notification: failed to persist notification for "
            "distributor=%s event_type=%s",
            distributor.pk,
            event_type,
        )
        return

    try:
        _push_live(notification)
    except Exception:
        # Mirrors this codebase's established SMS/wallet-live-update
        # failure isolation exactly: the notification already persisted
        # correctly above, so a Channels/Redis hiccup here is a UI
        # staleness problem (the distributor sees it on next login/
        # reconnect), never a reason to treat the whole call as failed.
        logger.exception(
            "send_notification: failed to push live notification for distributor=%s",
            distributor.pk,
        )


def _push_live(notification):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    unread_count = Notification.objects.filter(
        distributor_id=notification.distributor_id, is_read=False
    ).count()
    async_to_sync(channel_layer.group_send)(
        notification_group_name(notification.distributor_id),
        {
            "type": "notification_push",
            "id": notification.pk,
            "event_type": notification.event_type,
            "message": notification.message,
            "created_at": notification.created_at.isoformat(),
            "unread_count": unread_count,
        },
    )


def push_unread_count_update(distributor):
    """Task 21d-iv, doubt-driven-development finding: mark-read/mark-all-
    read only ever changed the DB and the acting tab's own view of the
    badge -- a second open tab for the same distributor never learned
    the count changed until its next full page load. Broadcasts the
    fresh absolute count (never a client-side increment/decrement,
    which would drift the moment two tabs are open) to every connected
    client for this distributor. Same failure-isolation shape as
    _push_live: a Channels/Redis hiccup here is a UI-staleness problem,
    never a reason to fail the mark-read/mark-all-read request that
    triggered it."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        unread_count = Notification.objects.filter(
            distributor_id=distributor.pk, is_read=False
        ).count()
        async_to_sync(channel_layer.group_send)(
            notification_group_name(distributor.pk),
            {"type": "unread_count_update", "unread_count": unread_count},
        )
    except Exception:
        logger.exception(
            "push_unread_count_update: failed to push for distributor=%s",
            distributor.pk,
        )
