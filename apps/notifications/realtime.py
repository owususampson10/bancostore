"""Task 61c. Channels group name for the admin portal's notification bell."""


def admin_notification_group_name() -> str:
    """ONE shared group for every connected admin, not one per staff
    account -- see AdminNotification's own docstring for why "the admin"
    is a role here rather than a person.

    A fixed string, deliberately: there is no id to interpolate, so there
    is nothing a client could influence. Distinct prefix from
    apps.distributors.realtime's "notifications_{id}" and "wallet_{id}",
    so the three can never collide.
    """
    return "admin_notifications"
