from django.urls import reverse

import pytest

"""Regression guard for a real bug: base_store.html's header/footer CTAs
(Log In, Become a Distributor) pointed to href="#" instead of the real
account_login/distributors:register URLs, and it shipped unnoticed because
no test asserted where these links actually go. Renders through the home
page since base_store.html has no view of its own."""


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
