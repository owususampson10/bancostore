import importlib

from django.conf import settings
from django.core.cache import cache

import pytest


def _configured_session_store_class():
    # Mirrors how Django itself resolves the session backend internally
    # (django.contrib.sessions.middleware.SessionMiddleware) -- reads
    # whatever SESSION_ENGINE actually says right now, rather than
    # hardcoding a specific backend's SessionStore, which would test that
    # backend in isolation regardless of what's actually configured.
    return importlib.import_module(settings.SESSION_ENGINE).SessionStore


def test_session_engine_is_cached_db_not_plain_cache():
    """Task 30e: plain 'cache' has no DB fallback -- a Redis
    eviction/restart previously logged out every user platform-wide,
    including admin's mandatory-2FA state."""
    assert settings.SESSION_ENGINE == "django.contrib.sessions.backends.cached_db"


@pytest.mark.django_db
def test_a_session_survives_a_cache_clear():
    """Reproduces the exact failure mode a Redis eviction/restart would
    otherwise cause: with the old plain-cache engine, clearing the cache
    is indistinguishable from losing every session. cached_db must
    survive it via its DB fallback."""
    SessionStore = _configured_session_store_class()
    session = SessionStore()
    session["some_key"] = "some_value"
    session.save()
    session_key = session.session_key

    cache.clear()

    reloaded = SessionStore(session_key=session_key)
    assert reloaded["some_key"] == "some_value"
