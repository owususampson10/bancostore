from apps.accounts.permissions import is_distributor
from apps.distributors.models import Distributor


def unread_notification_count(request):
    """Task 21d-iv: powers the header bell badge on every page site-wide
    (wired into settings.TEMPLATES, mirroring apps.orders.context_processors
    ::cart_count's own precedent), not just the distributor dashboard.

    Because this runs globally, it must never assume a Distributor row
    exists just because is_distributor() (a group-membership check only)
    is true -- a doubt-driven-development review flagged that the same
    gap already documented elsewhere in this codebase (CLAUDE.md's Task
    15 entry: select_starter_pack/start_kyc_verification/dashboard) would
    500 EVERY page for such a user here, not just one view."""
    user = request.user
    if not (user.is_authenticated and is_distributor(user)):
        return {"unread_notification_count": 0}
    try:
        distributor = user.distributor
    except Distributor.DoesNotExist:
        return {"unread_notification_count": 0}
    return {
        "unread_notification_count": distributor.notifications.filter(
            is_read=False
        ).count()
    }
