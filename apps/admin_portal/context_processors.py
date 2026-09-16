"""Task 61c. Supplies the admin bell's initial unread count."""

from apps.notifications.models import AdminNotification


def admin_notification_badge(request):
    """The badge's count on a full page load. Live updates after that
    arrive over the WebSocket; this is what makes the badge correct
    before one connects (and at all, if Channels is unavailable).

    Guarded on is_staff so this costs one query for admins and ZERO for
    everyone else -- a context processor runs on every single request in
    the project, including every storefront page view, and an
    unconditional COUNT(*) there would be a real cost at this project's
    stated scale for a number no storefront visitor can see.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return {}
    return {"admin_unread_count": AdminNotification.unread_count()}
