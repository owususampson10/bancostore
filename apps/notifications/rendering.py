"""Task 48a. Renders an admin-editable NotificationTemplate body/subject
against a fixed, pre-computed context dict -- never str.format(**dict)
or an f-string built from admin-entered text, both of which allow
attribute/index access through the format spec (e.g. "{0.__class__}")
when the TEMPLATE STRING itself, not just the substituted values, comes
from an untrusted-ish source (an admin, not a developer). This regex
substitution is small and fully auditable: it only ever replaces a
recognized {{name}} token with a plain string looked up from `context`
-- nothing in the template text can execute code, access an attribute,
or read anything the caller didn't explicitly pass in. A name with no
matching context key is left as a literal, visible '{{name}}' in the
output rather than raising, matching this codebase's "a notification
failure must never break the business operation it describes"
philosophy elsewhere (apps.withdrawal.services._notify and friends)."""

import logging
import re

from .models import NotificationTemplate

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(text: str, context: dict) -> str:
    def _replace(match):
        name = match.group(1)
        if name in context:
            return str(context[name])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def render_or_default(key: str, context: dict, *, default_body: str) -> str:
    """Looks up the live NotificationTemplate row for `key` and renders
    its body; falls back to `default_body` (still rendered against the
    same context) if no row exists for that key. A missing row is a
    real, loggable anomaly -- every key this function is ever called
    with should have been pre-seeded -- but it must never be the reason
    a real OTP/withdrawal/KYC notification fails to send."""
    template = NotificationTemplate.objects.filter(key=key).first()
    if template is None:
        logger.warning(
            "render_or_default: no NotificationTemplate row for key=%s -- "
            "falling back to the hardcoded default. Was it deleted?",
            key,
        )
        return render_template(default_body, context)
    return render_template(template.body, context)
