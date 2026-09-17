"""Task 66. {% sms_credit_banner %} -- the SMS credit banner shown below the
admin portal header. Rendered only by templates/admin_portal/base_dashboard.html,
so storefront pages never pay for the lookup."""

from django import template

from apps.admin_portal.permissions import is_admin_portal_staff
from apps.notifications.sms_credit_status import (
    BANNER_DISMISSED_SESSION_KEY,
    credit_banner,
)

register = template.Library()


@register.inclusion_tag("admin_portal/_sms_credit_banner.html", takes_context=True)
def sms_credit_banner(context):
    request = context.get("request")
    if request is None or not is_admin_portal_staff(request.user):
        return {"banner": None}
    banner = credit_banner()
    if (
        banner is not None
        and request.session.get(BANNER_DISMISSED_SESSION_KEY) == banner.shortage_id
    ):
        banner = None  # closed for this shortage, in this login
    return {"banner": banner, "csrf_token": context.get("csrf_token")}
