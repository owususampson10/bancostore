"""
URL configuration for bancostore project.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from two_factor.admin import AdminSiteOTPRequired
from two_factor.urls import urlpatterns as two_factor_urls

from apps.accounts.views import AdminLoginView

# Mandatory 2FA for admin (Task 6, SPEC.md Section 2.3) — this must never be
# made conditional on a settings toggle (e.g. ADMIN_2FA_ENABLED), per
# SPEC.md Boundaries: "Never... bypass the 2FA requirement for admin, even
# temporarily". Swapping the class on the existing global admin.site
# singleton applies to every already-registered ModelAdmin without needing
# to touch each app's admin.py.
admin.site.__class__ = AdminSiteOTPRequired

urlpatterns = [
    path("admin/", admin.site.urls),
    # Shadows two_factor's own "account/login/" route below: same literal
    # path, listed first, so Django's resolver dispatches here instead of to
    # the packaged LoginView. This swaps in AdminAuthenticationForm for the
    # 'auth' step (see apps/accounts/views.py) without needing to fork the
    # rest of two_factor's URLs. reverse('two_factor:login') still resolves
    # correctly — it's generated from the untouched pattern below, which
    # has the identical path string.
    path("account/login/", AdminLoginView.as_view(), name="admin_login"),
    path("accounts/", include("allauth.urls")),
    path("distributors/", include("apps.distributors.urls")),
    path("", include(two_factor_urls)),
    path("silk/", include("silk.urls", namespace="silk")),
]

if settings.DEBUG:
    urlpatterns += [
        path("__debug__/", include("debug_toolbar.urls")),
    ]
