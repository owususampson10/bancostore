from django.conf import settings
from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialApp
from constance import config


def _configure_a_real_google_social_app():
    social_app = SocialApp.objects.create(
        provider="google",
        name="Google",
        client_id="test-client-id",
        secret="test-secret",
    )
    social_app.sites.add(settings.SITE_ID)
    return social_app


@pytest.mark.django_db
def test_google_button_hidden_when_flag_disabled_even_with_a_social_app_configured(
    client,
):
    """Task 30c: GOOGLE_LOGIN_CUSTOMERS_ENABLED was previously fully
    decorative -- confirms an admin can now actually hide the button even
    when a SocialApp is configured."""
    _configure_a_real_google_social_app()
    config.GOOGLE_LOGIN_CUSTOMERS_ENABLED = False

    response = client.get(reverse("account_login"))

    assert b"Continue with Google" not in response.content


@pytest.mark.django_db
def test_google_button_shown_when_flag_enabled_and_a_social_app_is_configured(client):
    _configure_a_real_google_social_app()
    config.GOOGLE_LOGIN_CUSTOMERS_ENABLED = True

    response = client.get(reverse("account_login"))

    assert b"Continue with Google" in response.content


@pytest.mark.django_db
def test_google_button_stays_hidden_with_no_social_app_regardless_of_the_flag(client):
    """A flag alone can never satisfy allauth's own real DB dependency --
    toggling GOOGLE_LOGIN_CUSTOMERS_ENABLED=True with no SocialApp
    configured must not show a broken button."""
    config.GOOGLE_LOGIN_CUSTOMERS_ENABLED = True

    response = client.get(reverse("account_login"))

    assert b"Continue with Google" not in response.content


@pytest.mark.django_db
def test_signup_page_google_button_also_respects_the_flag(client):
    _configure_a_real_google_social_app()
    config.GOOGLE_LOGIN_CUSTOMERS_ENABLED = False

    response = client.get(reverse("account_signup"))

    assert b"Continue with Google" not in response.content
