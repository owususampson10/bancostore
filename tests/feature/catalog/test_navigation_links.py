from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

"""Regression guard for a real bug: base_store.html's header/footer CTAs
(Log In, Become a Distributor) pointed to href="#" instead of the real
account_login/distributors:register URLs, and it shipped unnoticed because
no test asserted where these links actually go. Renders through the home
page since base_store.html has no view of its own."""

User = get_user_model()


@pytest.mark.django_db
def test_log_in_link_points_to_real_login_page(client):
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert f'href="{reverse("account_login")}"' in content


@pytest.mark.django_db
def test_become_a_distributor_links_point_to_real_registration_page(client):
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    distributor_register_url = reverse("distributors:register")
    # Header, hero, footer, and CTA band all offer this link — assert it
    # appears more than once, not just that the URL exists somewhere.
    assert content.count(f'href="{distributor_register_url}"') >= 3


@pytest.mark.django_db
def test_logged_out_visitor_sees_log_in_and_become_a_distributor(client):
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "Log In" in content
    assert "Become a Distributor" in content


@pytest.mark.django_db
def test_logged_in_user_sees_my_orders_and_log_out_instead(client):
    """Task 25 (found while building the new order-history page): the
    header had no logged-in state at all -- a logged-in customer saw the
    identical Log In / Become a Distributor buttons as a guest, with no
    way to reach My Orders or log out except by typing a URL directly."""
    distributor_register_url = reverse("distributors:register")
    logged_out_count = (
        client.get(reverse("catalog:home"))
        .content.decode()
        .count(f'href="{distributor_register_url}"')
    )

    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert f'href="{reverse("orders:order_history")}"' in content
    # Log Out is a POST form, not a plain <a href> -- allauth's
    # LOGOUT_ON_GET defaults to False and this project never overrides it,
    # so a plain link would land on allauth's own confirmation page
    # instead of actually logging anyone out (caught in code review).
    # Assert method="post" and a CSRF token specifically, not just the
    # action URL -- a default-GET form pointed at the same URL would
    # still pass an action-only check while remaining broken (CodeRabbit).
    logout_url = reverse("account_logout")
    assert f'<form method="post" action="{logout_url}"' in content
    assert 'name="csrfmiddlewaretoken"' in content
    # Other "Become a Distributor" links (footer, hero/CTA band) are
    # standing marketing links, unrelated to auth state -- only the two
    # header instances (desktop + mobile) should disappear when logged in.
    assert content.count(f'href="{distributor_register_url}"') == logged_out_count - 2


@pytest.mark.django_db
def test_about_link_points_to_the_real_about_page(client):
    """Task 29: base_store.html's About link was a dead href="#" with
    title="Coming soon" in three places (desktop nav, mobile nav,
    footer). A 4th link was added later: the home page's "Discover
    Bancostore" bento's "Know everything about Bancostore" tile, itself a
    dead href="#" until it was wired to the About page in the same dead-
    link audit that fixed the admin login's Forgot password link."""
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    about_url = reverse("pages:about")
    # Desktop nav, mobile nav, footer "About Us", and the bento tile.
    assert content.count(f'href="{about_url}"') == 4


@pytest.mark.django_db
def test_contact_link_points_to_the_real_contact_page(client):
    """A 4th link (beyond desktop nav, mobile nav, footer) was added
    later: the home page's "Discover Bancostore" bento's "Contact Us"
    tile, itself a dead href="#" until it was wired to the Contact page in
    the same dead-link audit that fixed the admin login's Forgot password
    link."""
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    contact_url = reverse("pages:contact")
    assert content.count(f'href="{contact_url}"') == 4


@pytest.mark.django_db
def test_log_out_form_actually_logs_the_user_out(client):
    """The core bug: a plain <a href> to account_logout returns 200 with
    the user still authenticated (allauth's stock confirmation page,
    LOGOUT_ON_GET=False by default). Only a POST actually ends the
    session -- this proves the header's real logout button does that."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.post(reverse("account_logout"))

    assert response.status_code in (302, 200)
    assert "_auth_user_id" not in client.session
