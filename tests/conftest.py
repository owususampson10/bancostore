from django.core.cache import cache

import pytest


@pytest.fixture(autouse=True)
def _clear_django_cache():
    """django-constance is Redis-cached; pytest-django only rolls back the
    database between tests, so a value written to a constance setting in one
    test would otherwise leak into the next via Redis."""
    cache.clear()
    yield
    cache.clear()
