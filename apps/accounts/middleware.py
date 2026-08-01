import logging

from django.utils import timezone

from constance import config

logger = logging.getLogger(__name__)

# Session key holding the timestamp (ISO string) of the last time this
# middleware actually called set_expiry(). CodeRabbit finding: Django's
# request.session.get_expiry_age() does NOT decay over real elapsed
# time once set_expiry() is given a plain integer -- it stores that
# integer as-is in `_session_expiry` and returns it unchanged on every
# later call (confirmed against Django's own source; it only decays
# when set_expiry() is given a datetime/timedelta instead). Using
# get_expiry_age() to detect "has enough time passed to renew" is
# therefore permanently wrong for an integer-based expiry -- it would
# never trigger a second renewal, silently freezing the session's real
# absolute expiry at whatever it was computed as during the first
# renewal. Tracking elapsed time via our own timestamp sidesteps that
# entirely.
_RENEWED_AT_SESSION_KEY = "_session_timeout_renewed_at"


class SessionTimeoutMiddleware:
    """Idle-timeout session expiry (Task 30b) -- SESSION_TIMEOUT_MINUTES
    and ADMIN_SESSION_TIMEOUT_MINUTES were previously fully decorative
    (no SESSION_COOKIE_AGE override, no set_expiry() call anywhere), so
    every session used Django's hardcoded 2-week default regardless of
    role.

    request.session.set_expiry() already marks the session `modified`
    internally, so SESSION_SAVE_EVERY_REQUEST is deliberately NOT set --
    turning it on would force a Redis write on every anonymous storefront
    request too (the session-based cart, Task 17), not just authenticated
    dashboard traffic (doubt-driven-development finding).

    `is_staff` is this codebase's own established admin signal (matches
    apps/accounts/signals.py and apps/accounts/backends.py's existing
    lockout gating) -- not a new ad-hoc convention introduced here.

    Known limitation, matching the existing accepted gap already
    documented in apps/distributors/consumers.py for logout: an idle
    timeout swept here does not close an already-open WebSocket
    connection (WalletBalanceConsumer/NotificationConsumer authenticate
    once at handshake via Channels' AuthMiddlewareStack) -- a distributor
    or admin who leaves a dashboard tab open past the timeout keeps
    receiving live pushes on that tab until it's closed or refreshed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                minutes = (
                    config.ADMIN_SESSION_TIMEOUT_MINUTES
                    if request.user.is_staff
                    else config.SESSION_TIMEOUT_MINUTES
                )
            except Exception:
                # constance's cache backend (Redis) is unreachable --
                # leave this request's session expiry unchanged rather
                # than crashing every authenticated page view site-wide
                # during an outage.
                logger.warning(
                    "SessionTimeoutMiddleware: could not read session "
                    "timeout from constance, leaving session expiry "
                    "unchanged this request"
                )
            else:
                # Code-review finding: set_expiry() on every single
                # request forces a write on every authenticated request
                # (Redis, and -- since Task 30e's cached_db engine --
                # the django_session DB table too), the hottest path in
                # a system scoped for hundreds of thousands of users.
                # Only renew once real elapsed time since the last
                # renewal has passed half the window -- still guarantees
                # the idle timeout is enforced (a session is never more
                # than target seconds old when actually acted on), just
                # without a write on every single request. A session
                # with no renewal timestamp yet (new login, or one that
                # predates this middleware) always renews immediately.
                target_seconds = minutes * 60
                now = timezone.now()
                renewed_at_iso = request.session.get(_RENEWED_AT_SESSION_KEY)
                needs_renewal = True
                if renewed_at_iso:
                    renewed_at = timezone.datetime.fromisoformat(renewed_at_iso)
                    elapsed_seconds = (now - renewed_at).total_seconds()
                    needs_renewal = elapsed_seconds >= target_seconds / 2
                if needs_renewal:
                    request.session.set_expiry(target_seconds)
                    request.session[_RENEWED_AT_SESSION_KEY] = now.isoformat()
        return self.get_response(request)
