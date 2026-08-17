from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client, RequestFactory
from django.urls import reverse
from django.views.defaults import (
    bad_request,
    page_not_found,
    permission_denied,
    server_error,
)

import pytest

from bancostore.views import csrf_failure

pytestmark = pytest.mark.django_db


def _request_with_session(method, path):
    # 400/404/403 (and the CSRF failure view) pass the real request into their
    # template context, so every context processor in settings.py runs --
    # including apps.orders.context_processors.cart_count (needs
    # request.session) and apps.notifications.context_processors
    # ::unread_notification_count (needs request.user). A bare
    # RequestFactory request never runs SessionMiddleware/
    # AuthenticationMiddleware, so it has neither; attach both the same way
    # the real middleware stack would, rather than mocking the context
    # processors away.
    request = getattr(RequestFactory(), method)(path)
    SessionMiddleware(get_response=lambda r: None).process_request(request)
    AuthenticationMiddleware(get_response=lambda r: None).process_request(request)
    return request


def test_404_page_shows_page_not_found_and_a_way_back():
    request = _request_with_session("get", "/no-such-page/")

    response = page_not_found(request, exception=Http404())

    assert response.status_code == 404
    content = response.content.decode()
    assert "Page Not Found" in content
    assert reverse("catalog:home") in content
    assert reverse("catalog:product_list") in content


def test_500_page_shows_a_generic_message_and_a_way_back():
    request = RequestFactory().get("/whatever/")

    response = server_error(request)

    assert response.status_code == 500
    content = response.content.decode()
    assert "Something Went Wrong" in content
    assert reverse("catalog:home") in content


def test_403_page_shows_access_denied_and_contact_support():
    request = _request_with_session("get", "/forbidden/")

    response = permission_denied(request, exception=PermissionDenied())

    assert response.status_code == 403
    content = response.content.decode()
    assert "Access Denied" in content
    assert reverse("pages:contact") in content


def test_400_page_shows_bad_request_and_a_way_back():
    request = _request_with_session("get", "/bad/")

    response = bad_request(request, exception=Exception())

    assert response.status_code == 400
    content = response.content.decode()
    assert "Bad Request" in content
    assert reverse("catalog:home") in content


def test_csrf_failure_view_renders_the_session_expired_page():
    request = _request_with_session("post", "/whatever/")

    response = csrf_failure(request, reason="CSRF token missing.")

    assert response.status_code == 403
    content = response.content.decode()
    assert "Your Session Expired" in content
    assert "Refresh Page" in content


def test_a_real_csrf_failure_is_routed_through_the_themed_page(settings):
    # End-to-end check that CSRF_FAILURE_VIEW is actually wired in settings,
    # not just that the view function itself renders correctly in isolation.
    assert settings.CSRF_FAILURE_VIEW == "bancostore.views.csrf_failure"

    client = Client(enforce_csrf_checks=True)
    response = client.post(reverse("pages:contact"), data={})

    assert response.status_code == 403
    assert "Your Session Expired" in response.content.decode()
