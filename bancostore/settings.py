"""
Django settings for bancostore project.

See SPEC.md Tech Stack / Architecture for the rationale behind each piece wired up here.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

import dj_database_url

# These become Django settings by being module-level names here, not by being
# referenced below — the normal pattern for settings.py, not a real unused import.
from apps.platform_settings.config import (  # noqa: F401
    CONSTANCE_ADDITIONAL_FIELDS,
    CONSTANCE_CONFIG,
    CONSTANCE_CONFIG_FIELDSETS,
)

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without adding a new pip dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")


# Security
#
# Both of these fail CLOSED rather than open: if .env is missing, misnamed,
# or not loaded for any reason on a real host, the app must not silently
# fall back to DEBUG=True (exposes full tracebacks/settings to visitors) or
# a publicly-known SECRET_KEY (lets an attacker forge sessions, password
# reset tokens, and CSRF tokens). Local dev always has .env with both set
# explicitly, so this doesn't change anything for local dev.

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-local-dev-only")

DEBUG = os.environ.get("DEBUG", "False") == "True"

if not DEBUG and SECRET_KEY == "django-insecure-local-dev-only":
    raise ImproperlyConfigured(
        "SECRET_KEY is not set. Refusing to run with the insecure default "
        "outside DEBUG — set SECRET_KEY in the environment."
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",  # comma-formatted prices on the storefront (Task 8)
    # Required by FORM_RENDERER = TemplatesSetting below -- without this,
    # django.forms's own built-in widget templates (django/forms/widgets/
    # text.html etc.) aren't discoverable by our custom-DIRS engine, even
    # though the default renderer could always find them.
    "django.forms",
    # Real-time
    "channels",
    # Auth
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    "two_factor.plugins.phonenumber",
    "phonenumber_field",
    "formtools",
    # Settings / audit
    "constance",
    "constance.backends.database",
    "simple_history",
    # Frontend integration
    "django_htmx",
    # Queue
    "django_celery_beat",
    # Dev tooling (safe to always list; middleware below is gated on DEBUG)
    "debug_toolbar",
    "silk",
    # Bancostore apps
    "apps.accounts",
    "apps.catalog",
    "apps.distributors",
    "apps.notifications",
    "apps.platform_settings",
    "apps.binary_tree",
    "apps.pv_ledger",
    "apps.wallet",
    "apps.commissions",
    "apps.withdrawal",
    "apps.admin_portal",
    "apps.orders",
    "apps.pages",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must come after AuthenticationMiddleware (needs request.user) -- captures
    # who made a change on every HistoricalRecords()-tracked model save, not
    # just ones made through Django Admin. Added for Task 11's KYC audit
    # trail (apps/distributors/models.py::Distributor.history); CLAUDE.md
    # already called for this ("log admin actions affecting money or KYC
    # via django-simple-history") but no model had HistoricalRecords()
    # attached anywhere in the codebase until this retrospective.
    "simple_history.middleware.HistoryRequestMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
    # Must come after AuthenticationMiddleware (needs request.user).
    # Enforces the previously-decorative SESSION_TIMEOUT_MINUTES /
    # ADMIN_SESSION_TIMEOUT_MINUTES constance settings (Task 30b).
    "apps.accounts.middleware.SessionTimeoutMiddleware",
]

# django-ratelimit's middleware requires this — without it, a rate-limited
# request raises Ratelimited with no configured handler, itself crashing
# with an AttributeError instead of returning a clean response.
RATELIMIT_VIEW = "bancostore.views.ratelimited_view"

if DEBUG:
    MIDDLEWARE += [
        "debug_toolbar.middleware.DebugToolbarMiddleware",
        "silk.middleware.SilkyMiddleware",
    ]
    INTERNAL_IPS = ["127.0.0.1"]
    # sqlparse 0.5's hardened MAX_GROUPING_TOKENS limit crashes debug_toolbar's SQL
    # panel with a 500 on any request whose query is large enough to exceed it (e.g.
    # constance's bulk config lookup) — pure dev-tooling issue, so just skip the
    # pretty-printing step instead of pinning an older sqlparse.
    DEBUG_TOOLBAR_CONFIG = {"PRETTIFY_SQL": False}
    # silk defaults to open access (SILKY_AUTHENTICATION/SILKY_AUTHORISATION
    # are both False out of the box) and records full request bodies —
    # including plaintext passwords and OTP codes typed into login forms —
    # with no size cap. Its URL is only ever registered inside DEBUG (see
    # bancostore/urls.py), but require a logged-in superuser too, as
    # defense in depth in case DEBUG is ever left on somewhere it shouldn't be.
    SILKY_AUTHENTICATION = True
    SILKY_AUTHORISATION = True
    SILKY_PERMISSIONS = lambda user: user.is_superuser  # noqa: E731

# The stock ModelBackend is deliberately NOT listed here. It authenticates
# by the raw `username` field with no notion of AdminProfile.locked_until —
# and Django's createsuperuser naturally produces username == email if the
# operator types the same value at both prompts. Since authenticate() stops
# at the first backend that returns a user, ModelBackend being present at
# all (regardless of position) let it silently authenticate a locked-out
# admin whose username happened to equal their email, before the
# lockout-aware EmailBackend below ever got a chance to run. Permission
# checks (user.has_perm()) still work without it: EmailBackend and
# PhoneNumberBackend both subclass ModelBackend, so they inherit its
# has_perm()/has_module_perms() logic regardless of which one authenticated
# a given request — see tests/feature/accounts/test_admin_auth.py::
# test_lockout_holds_even_when_username_equals_email.
#
# apps.accounts.backends.EmailBackend must come before allauth's backend:
# allauth also matches by email (ACCOUNT_AUTHENTICATION_METHOD="email") with
# no concept of "staff-only" or lockout, so if it ran first it would happily
# authenticate a locked-out admin before our lockout check ever got a chance
# to raise PermissionDenied and stop the backend chain.
AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.EmailBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
    "apps.distributors.backends.PhoneNumberBackend",
]

SITE_ID = 1

# Bancostore is Ghana-only — without this, a locally-formatted number like
# "0545488681" (no +233) fails PhoneNumberField validation outright, since it
# has no way to know which country's numbering plan to check it against.
PHONENUMBER_DEFAULT_REGION = "GH"

# django-allauth — regular customer registration/login (see apps/accounts/forms.py for
# the custom signup form collecting full_name/phone_number, per docs Section 4.1).
# Distributor and admin login have their own rules, built in later tasks (5 and 6).
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "optional"
ACCOUNT_FORMS = {
    "signup": "apps.accounts.forms.CustomerSignupForm",
    "login": "apps.accounts.forms.CustomerLoginForm",
    "reset_password": "apps.accounts.forms.CustomerResetPasswordForm",
    "reset_password_from_key": "apps.accounts.forms.CustomerResetPasswordKeyForm",
}

# django-two-factor-auth patches admin's login to redirect through LOGIN_URL, so this
# must point at the 2FA-aware login view (admin requires mandatory 2FA — see SPEC.md).
LOGIN_URL = "two_factor:login"

# "Remember this device" (user-confirmed, 2026-07-30): an admin who has
# already completed a real TOTP proof on a given browser isn't re-asked
# for 7 days on that same browser -- a per-device, per-user, opt-in
# cookie (django-two-factor-auth's own built-in mechanism), NOT a way to
# disable 2FA. This is a different guarantee from
# test_2fa_requirement_cannot_be_bypassed_via_settings_toggle's own
# regression test (that one proves a config flag can never skip 2FA
# outright); the two are covered by separate tests in
# tests/feature/accounts/test_admin_auth.py. TWO_FACTOR_REMEMBER_COOKIE_SECURE
# is deliberately left at the library's False default -- this repo has no
# production security headers configured yet (see tasks/todo.md's Known
# issues), and a Secure-flagged cookie would silently never be sent over
# local dev's plain http://localhost. Revisit alongside SESSION_COOKIE_SECURE/
# CSRF_COOKIE_SECURE once Task 24 sets up real HTTPS.
TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7

# Themed "Session Expired" page instead of Django's raw technical CSRF
# error page. templates/404.html, 500.html, 403.html, 400.html need no
# equivalent setting -- Django's own default error handlers already pick
# those up by template name alone once the file exists.
CSRF_FAILURE_VIEW = "bancostore.views.csrf_failure"

ROOT_URLCONF = "bancostore.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.orders.context_processors.cart_count",
                "apps.notifications.context_processors.unread_notification_count",
                "apps.accounts.context_processors.google_login_flags",
            ],
        },
    },
]

# Django's default form renderer (django.forms.renderers.DjangoTemplates) uses
# its own isolated engine with DIRS=[] -- it can't see this project's
# templates/ directory above, so a form widget's custom template_name (e.g.
# CategoryImageWidget in apps/admin_portal/forms.py) fails with
# TemplateDoesNotExist even though the file is right where every other
# template in this project lives. TemplatesSetting instead reuses the real
# TEMPLATES config above, which is Django's own documented fix for this.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

WSGI_APPLICATION = "bancostore.wsgi.application"
ASGI_APPLICATION = "bancostore.asgi.application"


# Database
# Local dev defaults to SQLite; MySQL (via PyMySQL) is used in CI/production via
# DATABASE_URL. See SPEC.md Local dev environment for why MySQL doesn't run on this Mac.

import pymysql  # noqa: E402

pymysql.install_as_MySQLdb()

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        env="DATABASE_URL",
    )
}


# Cache / sessions — Redis-backed so app servers stay stateless (see SPEC.md Scale
# Architecture)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

# cached_db, not plain cache (Task 30e) -- plain cache has no DB fallback,
# so a Redis eviction/restart previously logged out every user
# platform-wide, including admin's mandatory-2FA state. django.contrib.
# sessions is already an installed app, so the DB-backed fallback table
# already exists -- no new migration needed.
#
# Honesty note (code-review finding): this protects against a cache
# eviction/restart (Django's cached_db.load() already catches a cache
# miss and reads through to the DB), but NOT against a genuine Redis
# *connectivity* outage -- django_redis isn't configured with
# IGNORE_EXCEPTIONS, so a save()/exists() call during a real outage
# still raises. "Eviction-resilient" is not the same claim as
# "outage-resilient."
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"


# Channels — WebSockets over the same Redis instance

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}


# Celery / Celery Beat

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND", "redis://localhost:6379/2"
)
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"


# django-constance — DB-backed, admin-editable business rules, Redis-cached so
# reads don't hit the database every time (CONSTANCE_CONFIG/FIELDSETS are imported
# above from apps.platform_settings.config — see SPEC.md Section 13/15).

CONSTANCE_BACKEND = "constance.backends.database.DatabaseBackend"
CONSTANCE_DATABASE_CACHE_BACKEND = "default"


# Password validation
#
# MinimumLengthValidator replaced with a live, constance-editable
# equivalent (Task 30a) -- the stock validator's min_length is fixed at
# process-start and can't reflect an admin's MIN_PASSWORD_LENGTH edit
# without a restart. ConfigurablePasswordComplexityValidator is new --
# PASSWORD_COMPLEXITY_ENABLED previously had no enforcement mechanism at
# all.

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "apps.accounts.validators.ConfigurableMinimumLengthValidator"},
    {"NAME": "apps.accounts.validators.ConfigurablePasswordComplexityValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Email — Gmail SMTP for password-reset links (customer decision, tasks/todo.md Task 4).
# Falls back to the console backend (prints emails to the terminal) when no Gmail
# credentials are set in .env, so local dev/tests never need real credentials for this.
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")

if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.gmail.com"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    # This Mac's python.org-installed Python doesn't pick up the system CA trust
    # store, so STARTTLS to Gmail fails with CERTIFICATE_VERIFY_FAILED without
    # this. certifi is already installed (a transitive dep of `requests`), so
    # this needs no new package — just point Python's default SSL context at it.
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@bancostore.test")


# mNotify — SMS OTP for distributor registration/login/password reset (Task 5).
# Falls back to a fake sender (apps/notifications/sms.py) when no API key is set in
# .env, so local dev/tests never spend real mNotify credit.
MNOTIFY_API_KEY = os.environ.get("MNOTIFY_API_KEY", "")
MNOTIFY_SENDER_ID = os.environ.get("MNOTIFY_SENDER_ID", "Bancostore")

# Paystack — registration fee / starter pack payments (Task 10). Environment
# variables, not django-constance: constance is plaintext in the database,
# fine for business-rule copy (PAYSTACK_PAYMENT_CHANNELS etc., still in
# apps/platform_settings/config.py) but not for real secret API keys.
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")

# Didit — distributor KYC verification (ID + selfie face-match/liveness), Task 11.
# Same reasoning as Paystack above: environment variables, not django-constance.
# DIDIT_WORKFLOW_ID identifies the verification workflow configured in Didit's
# dashboard (which checks it runs -- ID Verification + Face Match + Liveness),
# not a secret itself, but kept alongside the others since it's
# integration-specific, not a tunable business rule.
DIDIT_API_KEY = os.environ.get("DIDIT_API_KEY", "")
DIDIT_WEBHOOK_SECRET = os.environ.get("DIDIT_WEBHOOK_SECRET", "")
DIDIT_WORKFLOW_ID = os.environ.get("DIDIT_WORKFLOW_ID", "")


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files — Vite-built assets land in static/dist (see vite.config.js).
# static/images holds hand-placed assets (logos, favicon, app icons) that
# don't go through Vite — nothing imports/references them from main.js or
# main.css, so Vite's bundler would never copy them into static/dist.
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static" / "dist", BASE_DIR / "static" / "images"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files (Django FileField/ImageField + Pillow, local disk storage)

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
