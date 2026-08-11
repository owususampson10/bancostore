from django.core.cache import cache
from django.core.exceptions import ValidationError

import pytest

from apps.pages.models import SOCIAL_MEDIA_LINKS_CACHE_KEY, SocialMediaLink


def _kwargs(**overrides):
    defaults = {
        "name": "Facebook",
        "url": "https://facebook.com/bancostore",
        "platform": "facebook",
        "icon_color": "#0866FF",
    }
    defaults.update(overrides)
    return defaults


def _unsaved(**overrides):
    return SocialMediaLink(**_kwargs(**overrides))


@pytest.mark.django_db
class TestIconColorValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "#0866FF",
            "#0866ff",
            "#000000",
            "#FFFFFF",
        ],
    )
    def test_accepts_valid_six_digit_hex(self, value):
        _unsaved(icon_color=value).full_clean()  # must not raise

    @pytest.mark.parametrize(
        "value",
        [
            "#fff",  # 3-digit shorthand not supported
            "0866FF",  # missing "#"
            "#0866F",  # too short
            "#0866FF0",  # too long
            "rgba(8,102,255,1)",
            "#0866FG",  # not a hex digit
            "#0866FF; background: url(x)",  # injection attempt
            '#0866FF"',  # quote-breakout attempt
            "",
        ],
    )
    def test_rejects_invalid_values(self, value):
        with pytest.raises(ValidationError):
            _unsaved(icon_color=value).full_clean()


@pytest.mark.django_db
class TestPlatformChoices:
    def test_rejects_a_platform_slug_outside_the_curated_registry(self):
        with pytest.raises(ValidationError):
            _unsaved(platform="not-a-real-platform").full_clean()

    def test_accepts_the_custom_fallback_slug(self):
        _unsaved(platform="custom").full_clean()


@pytest.mark.django_db
class TestUrlValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "javascript:alert(document.cookie)",
            "data:text/html,<script>alert(1)</script>",
            "not a url",
        ],
    )
    def test_rejects_dangerous_or_malformed_schemes(self, value):
        with pytest.raises(ValidationError):
            _unsaved(url=value).full_clean()


@pytest.mark.django_db
class TestIconProperty:
    def test_resolves_to_the_matching_curated_icon(self):
        link = SocialMediaLink.objects.create(**_kwargs())
        assert link.icon.slug == "facebook"
        assert link.icon.svg_path


@pytest.mark.django_db
class TestCacheInvalidation:
    def test_creating_a_link_invalidates_the_cache(self):
        cache.set(SOCIAL_MEDIA_LINKS_CACHE_KEY, ["stale"], timeout=None)

        SocialMediaLink.objects.create(**_kwargs())

        assert cache.get(SOCIAL_MEDIA_LINKS_CACHE_KEY) is None

    def test_deleting_a_link_invalidates_the_cache(self):
        link = SocialMediaLink.objects.create(**_kwargs())
        cache.set(SOCIAL_MEDIA_LINKS_CACHE_KEY, ["stale"], timeout=None)

        link.delete()

        assert cache.get(SOCIAL_MEDIA_LINKS_CACHE_KEY) is None
