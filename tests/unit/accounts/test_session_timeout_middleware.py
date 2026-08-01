from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

import pytest
from constance import config

from apps.accounts.middleware import SessionTimeoutMiddleware

User = get_user_model()


def _request_for(user):
    request = Mock()
    request.user = user
    request.session = SessionStore()
    return request


@pytest.mark.django_db
def test_a_regular_users_session_expiry_matches_the_live_config_value():
    config.SESSION_TIMEOUT_MINUTES = 45
    user = User.objects.create_user(
        username="ama@example.test", password="pw", is_staff=False
    )
    request = _request_for(user)
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")

    middleware(request)

    assert request.session.get_expiry_age() == 45 * 60


@pytest.mark.django_db
def test_a_staff_users_session_expiry_matches_the_admin_config_value_instead():
    config.SESSION_TIMEOUT_MINUTES = 45
    config.ADMIN_SESSION_TIMEOUT_MINUTES = 10
    user = User.objects.create_user(
        username="admin@example.test", password="pw", is_staff=True
    )
    request = _request_for(user)
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")

    middleware(request)

    assert request.session.get_expiry_age() == 10 * 60


@pytest.mark.django_db
def test_does_not_re_renew_a_session_that_was_just_renewed():
    """Code-review finding: set_expiry() on every single request forces
    a Redis + django_session DB write (Task 30e's cached_db engine) on
    the hottest path in the app. Once a session has just been renewed
    (well within the configured window), a second call in quick
    succession must not force another write.

    CodeRabbit finding: request.session.get_expiry_age() does NOT decay
    over real elapsed time once set_expiry() is given a plain integer --
    Django stores that integer as-is and returns it unchanged on every
    later call (it only decays when set_expiry() is given a datetime/
    timedelta instead). An earlier version of this middleware/test used
    get_expiry_age() to detect drift, which is permanently wrong for an
    integer-based expiry -- verified against Django's own source. This
    test (and the one below) instead tracks elapsed real time by mocking
    django.utils.timezone.now(), matching how the middleware itself must
    track it."""
    config.SESSION_TIMEOUT_MINUTES = 45
    user = User.objects.create_user(username="ama@example.test", password="pw")
    request = _request_for(user)
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")

    middleware(request)
    with patch.object(request.session, "set_expiry") as mock_set_expiry:
        middleware(request)

    mock_set_expiry.assert_not_called()


@pytest.mark.django_db
def test_re_renews_once_real_elapsed_time_passes_half_the_window():
    config.SESSION_TIMEOUT_MINUTES = 45
    user = User.objects.create_user(username="ama@example.test", password="pw")
    request = _request_for(user)
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")
    now = timezone.now()

    with patch("apps.accounts.middleware.timezone.now", return_value=now):
        middleware(request)

    # Real wall-clock time has advanced past half the 45-minute window --
    # get_expiry_age() would still (wrongly) report the original 45*60
    # unchanged, since it never decays for an integer-based expiry. The
    # middleware must track this via its own stored timestamp instead.
    later = now + timedelta(minutes=23)
    with patch("apps.accounts.middleware.timezone.now", return_value=later):
        with patch.object(
            request.session, "set_expiry", wraps=request.session.set_expiry
        ) as mock_set_expiry:
            middleware(request)

    mock_set_expiry.assert_called_once_with(45 * 60)


@pytest.mark.django_db
def test_an_anonymous_requests_session_is_left_untouched():
    from django.contrib.auth.models import AnonymousUser

    request = _request_for(AnonymousUser())
    default_expiry = request.session.get_expiry_age()
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")

    middleware(request)

    assert request.session.get_expiry_age() == default_expiry


@pytest.mark.django_db
def test_middleware_calls_through_to_get_response_and_returns_its_result():
    user = User.objects.create_user(username="ama@example.test", password="pw")
    request = _request_for(user)
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "the real response")

    result = middleware(request)

    assert result == "the real response"


class _ExplodingConfig:
    """Stands in for constance's config proxy when its cache backend
    (Redis) is unreachable -- any attribute access raises."""

    def __getattr__(self, name):
        raise ConnectionError("redis down")


@pytest.mark.django_db
def test_falls_back_gracefully_if_constance_is_unreachable():
    """Security-review finding: a Redis outage must not crash every
    single authenticated request in the app -- the session's own expiry
    is simply left unchanged this request rather than raising.
    CodeRabbit finding: assert the fallback contract itself (expiry
    genuinely unchanged), not just that a response came back."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    request = _request_for(user)
    original_expiry_age = request.session.get_expiry_age()
    middleware = SessionTimeoutMiddleware(get_response=lambda r: "response")

    with patch("apps.accounts.middleware.config", new=_ExplodingConfig()):
        result = middleware(request)

    assert result == "response"
    assert request.session.get_expiry_age() == original_expiry_age
