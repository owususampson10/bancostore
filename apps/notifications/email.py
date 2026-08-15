"""Task 48d. Centralizes the "From" address every real send site uses,
so the constance-override-falls-back-to-the-validated-env-default logic
lives in exactly one place instead of being duplicated at each of the
six send_mail()/EmailMessage call sites this codebase has."""

from django.conf import settings

from constance import config


def get_sender_email() -> str:
    """SENDER_EMAIL_ADDRESS defaults to blank, meaning "use the server's
    configured default" -- settings.DEFAULT_FROM_EMAIL, which already
    has its own startup-time validation (bancostore/settings.py, Task
    24d) rejecting known-insecure placeholder values. An admin can
    still deliberately override it live by setting a non-blank value;
    that override is not re-validated against the same placeholder
    list, since this is a trusted admin action, not a deploy-time
    environment misconfiguration -- the two threats Task 24d's check
    and this fallback each guard against are genuinely different."""
    return config.SENDER_EMAIL_ADDRESS or settings.DEFAULT_FROM_EMAIL
