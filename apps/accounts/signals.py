from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.mail import send_mail
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from constance import config

from apps.notifications.email import get_sender_email
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import AdminProfile

User = get_user_model()


@receiver(user_login_failed)
def lock_admin_after_repeated_failures(sender, credentials, request=None, **kwargs):
    """Wrong-password lockout + alert email for admin logins (Task 6),
    mirroring the distributor lockout in apps/distributors/services.py.
    Fires for every failed authenticate() call across all backends (customer,
    distributor, admin), so it must filter to admin accounts itself rather
    than relying on which view triggered it.

    The whole get_or_create-then-increment sequence is wrapped in one
    retry_on_lock_contention() call — a real multi-threaded test reproduced
    a lost-update race here under concurrent failed logins before this fix
    (see tests/feature/accounts/test_admin_auth.py::
    test_concurrent_failed_logins_do_not_lose_increments). Note
    get_or_create() alone is NOT enough even though Django handles its
    create-race internally: that internal handling only covers
    IntegrityError (two inserts colliding), not OperationalError (SQLite's
    "database table is locked" under concurrent writes) — the first fix
    attempt here left get_or_create() outside the retry wrapper reasoning
    it was "safe on its own," and a 15-run flake-rate check (2/15 failures)
    proved that reasoning wrong before this version was written."""
    email = credentials.get("username")
    if not email:
        return
    try:
        user = User.objects.get(email__iexact=email, is_staff=True)
    except (User.DoesNotExist, User.MultipleObjectsReturned):
        return

    should_alert = False

    def _attempt():
        nonlocal should_alert
        with transaction.atomic():
            AdminProfile.objects.get_or_create(user=user)
            profile = select_for_update_nowait_if_supported(AdminProfile.objects).get(
                user=user
            )
            now = timezone.now()

            if profile.locked_until and profile.locked_until > now:
                # Already locked — don't extend the lock or re-alert on
                # every additional attempt while it's still in effect.
                return

            if profile.locked_until and profile.locked_until <= now:
                profile.failed_login_attempts = 0
                profile.locked_until = None

            profile.failed_login_attempts += 1
            if profile.failed_login_attempts >= config.MAX_FAILED_LOGIN_ATTEMPTS:
                profile.locked_until = now + timedelta(
                    minutes=config.ACCOUNT_LOCKOUT_DURATION_MINUTES
                )
                should_alert = True
            profile.save(update_fields=["failed_login_attempts", "locked_until"])

    retry_on_lock_contention(_attempt)
    if should_alert:
        _send_lockout_alert(user)


@receiver(user_logged_in)
def reset_admin_failed_attempts(sender, user, request=None, **kwargs):
    if not user.is_staff:
        return
    AdminProfile.objects.filter(user=user).update(
        failed_login_attempts=0, locked_until=None
    )


def _send_lockout_alert(user):
    if not config.LOCKOUT_ALERT_EMAIL:
        return
    send_mail(
        subject="Bancostore admin account locked",
        message=(
            f"The admin account {user.email} was locked after too many "
            "failed login attempts."
        ),
        from_email=get_sender_email(),
        recipient_list=[config.LOCKOUT_ALERT_EMAIL],
    )
