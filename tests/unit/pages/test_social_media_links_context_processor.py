from unittest.mock import patch

from django.test import RequestFactory

import pytest

from apps.pages.context_processors import social_media_links
from apps.pages.models import SOCIAL_MEDIA_LINKS_CACHE_KEY, SocialMediaLink


def _request():
    return RequestFactory().get("/")


def _make(**overrides):
    defaults = {
        "name": "Facebook",
        "url": "https://facebook.com/bancostore",
        "platform": "facebook",
        "icon_color": "#0866FF",
        "order": 1,
    }
    defaults.update(overrides)
    return SocialMediaLink.objects.create(**defaults)


@pytest.mark.django_db
def test_returns_configured_links():
    link = _make()

    result = social_media_links(_request())

    assert list(result["social_media_links"]) == [link]


@pytest.mark.django_db
def test_returns_empty_list_when_none_configured():
    result = social_media_links(_request())

    assert list(result["social_media_links"]) == []


@pytest.mark.django_db
def test_falls_back_to_the_database_when_the_cache_read_fails():
    """CodeRabbit finding: django_redis raises on a real connection
    failure rather than degrading gracefully -- left uncaught, this
    context processor running on every single page would 500 the whole
    storefront on a Redis outage, not just this footer section."""
    link = _make()

    with patch("apps.pages.context_processors.cache.get", side_effect=ConnectionError):
        result = social_media_links(_request())

    assert list(result["social_media_links"]) == [link]


@pytest.mark.django_db
def test_still_returns_links_when_the_cache_write_fails():
    link = _make()

    with patch("apps.pages.context_processors.cache.set", side_effect=ConnectionError):
        result = social_media_links(_request())

    assert list(result["social_media_links"]) == [link]


@pytest.mark.django_db
def test_deleting_a_link_does_not_500_when_the_cache_delete_fails():
    """Mirrors the context processor's own resilience -- a Redis hiccup
    during the post_delete signal must not surface as a 500 on the
    admin's delete action when the database write already succeeded."""
    link = _make()

    with patch("apps.pages.models.cache.delete", side_effect=ConnectionError):
        link.delete()  # must not raise

    assert not SocialMediaLink.objects.filter(pk=link.pk).exists()


@pytest.mark.django_db
def test_creating_a_link_does_not_500_when_the_cache_delete_fails():
    with patch("apps.pages.models.cache.delete", side_effect=ConnectionError):
        _make()  # must not raise

    assert SocialMediaLink.objects.count() == 1


@pytest.mark.django_db
def test_cache_key_constant_is_used_for_both_read_and_write():
    _make()

    with patch("apps.pages.context_processors.cache.set") as mock_set:
        social_media_links(_request())

    assert mock_set.call_args.args[0] == SOCIAL_MEDIA_LINKS_CACHE_KEY
