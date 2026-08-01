import logging

from constance import config

logger = logging.getLogger(__name__)


def google_login_flags(request):
    """Task 30c: GOOGLE_LOGIN_CUSTOMERS_ENABLED was previously fully
    decorative. allauth's own account_login/account_signup views render
    the Google button from the stock {% get_providers %} tag, which
    checks only whether a SocialApp row exists -- an admin couldn't hide
    the button without deleting that row. Runs globally (mirroring
    apps.orders.context_processors::cart_count's precedent) since
    allauth's built-in views aren't overridden in this codebase, so a
    context processor is the only way to thread this flag into their
    templates.
    """
    try:
        enabled = config.GOOGLE_LOGIN_CUSTOMERS_ENABLED
    except Exception:
        # This runs on every single page load site-wide, so a Redis blip
        # must never crash the whole site -- default to the setting's
        # own seeded default (True) rather than raising. Fail-OPEN is
        # correct here: this only ever hides/shows a cosmetic login
        # button (contrast with validators.py's fail-CLOSED default,
        # which guards an actual security control).
        logger.warning(
            "google_login_flags: could not read GOOGLE_LOGIN_CUSTOMERS_ENABLED "
            "from constance, defaulting to enabled"
        )
        enabled = True
    return {"google_login_customers_enabled": enabled}
