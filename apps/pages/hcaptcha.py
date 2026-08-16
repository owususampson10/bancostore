import logging

from django.conf import settings

import requests

logger = logging.getLogger(__name__)

HCAPTCHA_VERIFY_URL = "https://hcaptcha.com/siteverify"
REQUEST_TIMEOUT_SECONDS = 5


def hcaptcha_enabled():
    """Same blank-means-disabled convention as MNOTIFY_API_KEY: an admin
    hasn't set up an hCaptcha account yet, so the widget doesn't render
    and verification is skipped entirely -- never a hard failure."""
    return bool(settings.HCAPTCHA_SITE_KEY and settings.HCAPTCHA_SECRET_KEY)


def verify_hcaptcha(token):
    """Source: https://docs.hcaptcha.com/#verify-the-user-response-server-side
    POST secret + response to /siteverify, check "success". Fails closed
    (rejects) on a missing token or any network/response error -- unlike
    Paystack's wrapper, which raises so the caller can decide, a CAPTCHA
    check has exactly one safe default when it can't be confirmed: treat
    the submission as unverified rather than silently letting it through."""
    if not hcaptcha_enabled():
        return True
    if not token:
        return False
    try:
        response = requests.post(
            HCAPTCHA_VERIFY_URL,
            data={"secret": settings.HCAPTCHA_SECRET_KEY, "response": token},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return bool(response.json().get("success"))
    except requests.RequestException:
        logger.exception("hcaptcha: verification request failed")
        return False
