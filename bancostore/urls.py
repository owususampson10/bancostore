"""
URL configuration for bancostore project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path
from django.views.generic import TemplateView

from two_factor.admin import AdminSiteOTPRequired
from two_factor.urls import urlpatterns as two_factor_urls

from apps.accounts.views import AdminLoginView, admin_portal_permission_denied
from apps.pages.sitemaps import sitemaps

# Redirects a brand-new admin (zero confirmed 2FA devices yet) straight to
# 2FA setup instead of a bare 403 the first time they hit any admin_portal
# page -- see admin_portal_permission_denied's own docstring. Site-wide by
# necessity (Django's handler403 has no URL-prefix scoping), but the
# handler's own condition (is_staff + zero confirmed devices) means it can
# only ever fire for a genuine admin account, never a customer/distributor
# hitting an unrelated 403.
handler403 = admin_portal_permission_denied

# Mandatory 2FA for admin (Task 6, SPEC.md Section 2.3) — this must never be
# made conditional on a settings toggle (e.g. ADMIN_2FA_ENABLED), per
# SPEC.md Boundaries: "Never... bypass the 2FA requirement for admin, even
# temporarily". Swapping the class on the existing global admin.site
# singleton applies to every already-registered ModelAdmin without needing
# to touch each app's admin.py.
admin.site.__class__ = AdminSiteOTPRequired

urlpatterns = [
    path(
        "robots.txt",
        TemplateView.as_view(template_name="robots.txt", content_type="text/plain"),
        name="robots_txt",
    ),
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="sitemap"),
    # Google Search Console domain-ownership verification (URL-prefix
    # method) — the file's content and its exact URL path are both fixed by
    # Google, not chosen here. Served the same way as robots.txt above
    # (a literal-content TemplateView) rather than as a static file, since
    # a static file living outside STATIC_URL's "static/" prefix wouldn't
    # be reachable at the required bare root path.
    path(
        "google046e53e7ef60a9ba.html",
        TemplateView.as_view(
            template_name="google046e53e7ef60a9ba.html", content_type="text/html"
        ),
        name="google_site_verification",
    ),
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
    # Task 40a: a deliberately distinct "addresses/" prefix, not "account/"
    # (already the two_factor/admin-2FA namespace, per this codebase's own
    # documented accounts/ vs account/ mix-up gotcha) or "accounts/"
    # (allauth's own prefix above).
    path("addresses/", include("apps.accounts.urls")),
    path("distributors/", include("apps.distributors.urls")),
    path("admin-portal/", include("apps.admin_portal.urls")),
    path("cart/", include("apps.orders.urls")),
    path("", include("apps.pages.urls")),
    path("", include("apps.catalog.urls")),
    path("", include(two_factor_urls)),
]

if settings.DEBUG:
    # silk was previously registered unconditionally above, with no
    # SILKY_AUTHENTICATION/SILKY_AUTHORISATION set — its dashboard defaults
    # to open access and records full request bodies (passwords, OTP codes)
    # with no size cap, so it must never be reachable outside DEBUG. Even
    # with DEBUG on, SILKY_AUTHENTICATION/SILKY_AUTHORISATION (settings.py)
    # now require a logged-in superuser to view it.
    urlpatterns += [
        path("silk/", include("silk.urls", namespace="silk")),
        path("__debug__/", include("debug_toolbar.urls")),
    ]
    # django.contrib.staticfiles auto-serves STATIC_URL under runserver, but
    # MEDIA_URL (uploaded product photos, Task 7) needs this explicit route —
    # without it every /media/... request 404s even though the file exists
    # on disk. Production serves media via Nginx directly (Task 24), so this
    # stays DEBUG-only. This line has no pytest coverage and cannot get any:
    # Django's test runner always forces DEBUG=False (documented, so tests
    # reflect production), so this whole `if DEBUG:` block never executes
    # under pytest regardless of .env — verify by curling a real /media/ URL
    # against the actual runserver, not by writing a test for it.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
