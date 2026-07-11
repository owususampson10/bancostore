from django.core.cache import cache

import pytest

from apps.notifications.sms import fake_outbox


@pytest.fixture(autouse=True)
def _clear_django_cache():
    """django-constance is Redis-cached; pytest-django only rolls back the
    database between tests, so a value written to a constance setting in one
    test would otherwise leak into the next via Redis."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _clear_fake_sms_outbox():
    """fake_outbox is a plain module-level list (mirrors django.core.mail.outbox),
    so nothing resets it between tests automatically — same class of leak as the
    Redis-backed constance cache above."""
    fake_outbox.clear()
    yield
    fake_outbox.clear()


@pytest.fixture(autouse=True)
def _force_fake_sms_sender(settings):
    """Django's test runner automatically forces EMAIL_BACKEND to locmem
    regardless of what's in .env, so real Gmail sends never happen in tests —
    but there's no equivalent built-in protection for our own MNOTIFY_API_KEY
    setting. Once a real key is added to .env (Task 5's final verification
    step), every test run started silently hitting the real mNotify API
    instead of the fake sender. Force it empty here so tests can never spend
    real SMS credit, no matter what's configured locally."""
    settings.MNOTIFY_API_KEY = ""
