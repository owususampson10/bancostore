import logging

from constance import config

logger = logging.getLogger(__name__)


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
                # Only renew once the remaining age has drifted outside
                # [target/2, target] -- a fresh/never-set session starts
                # at Django's 2-week SESSION_COOKIE_AGE default (well
                # above target, so it renews immediately), and afterwards
                # only renews again once genuinely more than half the
                # window has elapsed. Still guarantees the idle timeout
                # is enforced (a session is never more than target
                # seconds old when actually acted on), just without a
                # write on every single request.
                target_seconds = minutes * 60
                current_age = request.session.get_expiry_age()
                if not (target_seconds / 2 <= current_age <= target_seconds):
                    request.session.set_expiry(target_seconds)
        return self.get_response(request)
