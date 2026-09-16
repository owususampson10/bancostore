import logging

from django.db import transaction

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.distributors.realtime import notification_group_name

from .models import Notification
from .realtime import admin_notification_group_name

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
    # CodeRabbit finding (PR #79): Notification.message is max_length=500,
    # but a rendered NotificationTemplate body (Task 48c) has no length
    # bound of its own -- an admin-edited template, or a long substituted
    # value (e.g. a distributor's full_name), could exceed it. This
    # already runs after the caller's own transaction has committed (see
    # send_notification's on_commit deferral above) and the whole call is
    # already wrapped in try/except below, so an over-length value could
    # never roll back the placement/bonus/etc. event that triggered it --
    # but it would silently drop the notification entirely once caught.
    # Truncating up front means the distributor still gets a (slightly
    # shortened) notification instead of losing it.
    max_length = Notification._meta.get_field("message").max_length
    if len(message) > max_length:
        message = message[:max_length]
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


def send_admin_notification(event_type, message, order=None):
    """Task 61c. Records an admin-portal bell notification and pushes it
    live to every connected admin.

    Never raises. This is called from the order-confirmation path after
    money, stock and PV have already committed, so a failure to write a
    bell row must never look like the order itself failed -- the same
    contract every other notification site in this codebase has. Returns
    the row, or None if it could not be recorded.

    The push is deferred to transaction.on_commit for the same reason
    send_notification above defers: pushing from inside an open
    transaction can announce an event that then rolls back, and a bell
    showing an order that does not exist is worse than a bell that is a
    moment late.
    """
    from .models import AdminNotification

    try:
        notification = AdminNotification.objects.create(
            event_type=event_type, message=message, order=order
        )
    except Exception:
        logger.exception(
            "send_admin_notification: could not record %s -- the caller's "
            "own work is unaffected.",
            event_type,
        )
        return None

    def _push():
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                return
            async_to_sync(channel_layer.group_send)(
                admin_notification_group_name(),
                {
                    "type": "notification_push",
                    "id": notification.pk,
                    "event_type": notification.event_type,
                    "message": notification.message,
                    "created_at": notification.created_at.isoformat(),
                    "unread_count": AdminNotification.unread_count(),
                },
            )
        except Exception:
            logger.exception(
                "send_admin_notification: recorded %s but could not push it "
                "live. It will still appear on the next page load.",
                notification.pk,
            )

    transaction.on_commit(_push)
    return notification


def broadcast_admin_unread_count():
    """Task 61c. Cross-tab badge reconciliation: one admin marking the
    bell read in one tab must not leave a stale badge in another, or in
    another admin's browser.

    Never raises -- a badge that is briefly out of date is not worth
    failing a request over, and this is called from a view that has
    already done its real work by the time it runs.
    """
    from .models import AdminNotification

    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            admin_notification_group_name(),
            {
                "type": "unread_count_update",
                "unread_count": AdminNotification.unread_count(),
            },
        )
    except Exception:
        logger.exception(
            "broadcast_admin_unread_count: could not push the updated count. "
            "Badges will correct themselves on the next page load."
        )
