"""
Django settings for bancostore project.

See SPEC.md Tech Stack / Architecture for the rationale behind each piece wired up here.
"""

import os
from pathlib import Path

import dj_database_url

# These become Django settings by being module-level names here, not by being
# referenced below — the normal pattern for settings.py, not a real unused import.
from apps.platform_settings.config import (  # noqa: F401
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

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-local-dev-only")

DEBUG = os.environ.get("DEBUG", "True") == "True"

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
    "apps.distributors",
    "apps.platform_settings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
]

if DEBUG:
    MIDDLEWARE += [
        "debug_toolbar.middleware.DebugToolbarMiddleware",
        "silk.middleware.SilkyMiddleware",
    ]
    INTERNAL_IPS = ["127.0.0.1"]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

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
            ],
        },
    },
]

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

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
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

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
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
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@bancostore.test")


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files — Vite-built assets land in static/dist (see vite.config.js)

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static" / "dist"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files (Django FileField/ImageField + Pillow, local disk storage)

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
