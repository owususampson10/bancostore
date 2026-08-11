import pytest

from apps.pages.social_icons import SOCIAL_ICONS, detect_platform_from_url, get_icon


class TestDetectPlatformFromUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://facebook.com/bancostore", "facebook"),
            ("https://www.facebook.com/bancostore", "facebook"),
            ("http://facebook.com/bancostore", "facebook"),
            ("facebook.com/bancostore", "facebook"),
            ("https://business.facebook.com/bancostore", "facebook"),
            ("https://instagram.com/bancostore", "instagram"),
            ("https://x.com/bancostore", "x"),
            ("https://twitter.com/bancostore", "x"),
            ("https://youtube.com/@bancostore", "youtube"),
            ("https://youtu.be/abc123", "youtube"),
            ("https://wa.me/233123456789", "whatsapp"),
            ("https://t.me/bancostore", "telegram"),
        ],
    )
    def test_matches_known_platform_domains(self, url, expected):
        assert detect_platform_from_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            None,
            "not a url at all",
            "https://example.com",
            "https://mybusiness.com/facebook",
        ],
    )
    def test_falls_back_to_custom_for_unknown_or_invalid_input(self, url):
        assert detect_platform_from_url(url) == "custom"

    def test_linkedin_is_not_a_curated_icon(self):
        """Code-review finding (2026-08-11), user-confirmed: LinkedIn's
        own trademark enforcement got it permanently removed from Simple
        Icons in v14.0.0. Bancostore deliberately doesn't self-host a copy
        of a mark the rights holder had removed elsewhere -- a LinkedIn
        link falls back to the generic "custom" icon, same as any other
        uncurated platform."""
        assert "linkedin" not in SOCIAL_ICONS
        assert (
            detect_platform_from_url("https://linkedin.com/company/bancostore")
            == "custom"
        )

    def test_does_not_match_a_lookalike_domain_as_a_substring(self):
        """A real spoofing-shaped attempt: "facebook.com" appearing as a
        substring of an unrelated domain must never be treated as a match
        -- only an exact domain or a true subdomain counts."""
        assert (
            detect_platform_from_url("https://facebook.com.evil.example/") == "custom"
        )
        assert detect_platform_from_url("https://notfacebook.com/") == "custom"
        assert detect_platform_from_url("https://evilfacebook.com/") == "custom"

    def test_strips_userinfo_before_matching_the_host(self):
        """https://facebook.com@evil.com/ -- the real host is evil.com, not
        facebook.com. urlparse's netloc is "facebook.com@evil.com"; the
        part after "@" is what actually gets requested by a browser."""
        assert detect_platform_from_url("https://facebook.com@evil.com/") == "custom"

    def test_strips_port_before_matching_the_host(self):
        assert detect_platform_from_url("https://facebook.com:8443/page") == "facebook"

    def test_query_string_and_fragment_do_not_leak_into_host_matching(self):
        assert detect_platform_from_url("https://evil.com?x=facebook.com") == "custom"
        assert detect_platform_from_url("https://evil.com#facebook.com") == "custom"

    def test_scheme_less_url_with_a_double_slash_in_the_path_still_matches(self):
        """CodeRabbit finding: a bare "//" in url check misdetects a
        scheme-less URL whose *path* contains "//" as already having a
        scheme, skipping the "//" prefix urlparse needs to find a netloc
        at all -- this used to incorrectly fall back to "custom"."""
        assert detect_platform_from_url("facebook.com/page//photos") == "facebook"

    def test_already_protocol_relative_url_is_not_double_prefixed(self):
        assert detect_platform_from_url("//facebook.com/page") == "facebook"


class TestGetIcon:
    def test_returns_the_registered_icon(self):
        icon = get_icon("facebook")
        assert icon.label == "Facebook"
        assert icon.svg_path

    def test_falls_back_to_custom_for_an_unknown_slug(self):
        icon = get_icon("some-slug-that-does-not-exist")
        assert icon is SOCIAL_ICONS["custom"]

    def test_custom_fallback_has_no_svg_path(self):
        """No svg_path means the render partial falls back to the Material
        Symbols glyph instead of an empty/broken <svg><path> element."""
        assert SOCIAL_ICONS["custom"].svg_path == ""


class TestSocialIconRegistryIntegrity:
    """Every curated icon must actually be usable -- these aren't
    behavioral tests so much as a guard against a future data-entry typo
    in social_icons.py silently shipping a broken icon."""

    def test_every_icon_has_a_label_and_a_valid_hex_default_color(self):
        import re

        hex_pattern = re.compile(r"\A#[0-9A-Fa-f]{6}\Z")
        for slug, icon in SOCIAL_ICONS.items():
            assert icon.slug == slug
            assert icon.label
            assert hex_pattern.match(icon.default_color), slug

    def test_every_non_custom_icon_has_an_svg_path_and_domains(self):
        for slug, icon in SOCIAL_ICONS.items():
            if slug == "custom":
                continue
            assert icon.svg_path, slug
            assert icon.domains, slug
