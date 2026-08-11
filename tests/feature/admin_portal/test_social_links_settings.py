from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from constance import config

from apps.admin_portal.views import _general_settings_tab_index
from apps.pages.models import SocialMediaLink

User = get_user_model()


def _settings_url():
    """Task 36c: no standalone page anymore -- Social Links lives inside
    Platform Settings' General tab."""
    return reverse("admin_portal:platform_settings")


def _general_settings_url():
    """CodeRabbit finding: the redirect tests below only checked *a* tab
    query param was present, which would still pass for a redirect to the
    wrong tab -- computed from the same helper the view itself uses
    (apps.admin_portal.views._general_settings_tab_index) rather than a
    hardcoded index, so it can't silently drift if CONSTANCE_CONFIG_
    FIELDSETS is ever reordered."""
    return f"{_settings_url()}?tab={_general_settings_tab_index()}"


def _create_url():
    return reverse("admin_portal:social_link_create")


def _update_url(link):
    return reverse("admin_portal:social_link_update", args=[link.pk])


def _delete_url(link):
    return reverse("admin_portal:social_link_delete", args=[link.pk])


def _detect_url():
    return reverse("admin_portal:social_link_detect_platform")


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


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_settings_page(client, db):
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    response = client.get(_settings_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_detect_endpoint(client, db):
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    response = client.get(_detect_url(), {"url": "https://facebook.com/x"})

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_settings_url())

    assert response.status_code == 302


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_settings_page_shows_existing_links(staff_client):
    _make(name="Bancostore on Facebook")

    response = staff_client.get(_settings_url())

    assert response.status_code == 200
    assert b"Bancostore on Facebook" in response.content


@pytest.mark.django_db
def test_settings_page_shows_an_empty_state_with_no_links(staff_client):
    response = staff_client.get(_settings_url())

    assert b"No social links yet" in response.content


@pytest.mark.django_db
def test_settings_page_no_longer_has_its_own_sidebar_item(staff_client):
    """Task 36c: moved into Platform Settings' General tab, per explicit
    user request -- the old standalone sidebar entry must be gone."""
    response = staff_client.get(_settings_url())

    assert b"Social Links</span>" not in response.content


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_creating_a_link_persists_it(staff_client):
    response = staff_client.post(
        _create_url(),
        {
            "name": "Bancostore",
            "url": "https://instagram.com/bancostore",
            "platform": "instagram",
            "icon_color": "#FF0069",
        },
        follow=True,
    )

    assert response.status_code == 200
    link = SocialMediaLink.objects.get(name="Bancostore")
    assert link.platform == "instagram"
    assert link.icon_color == "#FF0069"


@pytest.mark.django_db
def test_creating_a_link_redirects_to_the_general_settings_tab(staff_client):
    response = staff_client.post(
        _create_url(),
        {
            "name": "Bancostore",
            "url": "https://instagram.com/bancostore",
            "platform": "instagram",
            "icon_color": "#FF0069",
        },
    )

    assert response.status_code == 302
    assert response.url == _general_settings_url()


@pytest.mark.django_db
def test_creating_a_link_assigns_the_next_order(staff_client):
    _make(name="First", order=5)

    staff_client.post(
        _create_url(),
        {
            "name": "Second",
            "url": "https://instagram.com/bancostore",
            "platform": "instagram",
            "icon_color": "#FF0069",
        },
    )

    second = SocialMediaLink.objects.get(name="Second")
    assert second.order == 6


@pytest.mark.django_db
def test_creating_a_link_with_an_invalid_hex_color_shows_a_form_error(staff_client):
    response = staff_client.post(
        _create_url(),
        {
            "name": "Bad Color",
            "url": "https://instagram.com/bancostore",
            "platform": "instagram",
            "icon_color": "not-a-color",
        },
    )

    assert response.status_code == 400
    assert not SocialMediaLink.objects.filter(name="Bad Color").exists()


@pytest.mark.django_db
def test_creating_a_link_with_a_javascript_url_is_rejected(staff_client):
    response = staff_client.post(
        _create_url(),
        {
            "name": "Malicious",
            "url": "javascript:alert(document.cookie)",
            "platform": "custom",
            "icon_color": "#FFFFFF",
        },
    )

    assert response.status_code == 400
    assert not SocialMediaLink.objects.filter(name="Malicious").exists()


@pytest.mark.django_db
def test_cannot_create_a_link_beyond_the_max_cap(staff_client):
    for i in range(config.MAX_SOCIAL_MEDIA_LINKS):
        _make(name=f"Link {i}", order=i)

    response = staff_client.post(
        _create_url(),
        {
            "name": "One Too Many",
            "url": "https://instagram.com/bancostore",
            "platform": "instagram",
            "icon_color": "#FF0069",
        },
    )

    assert response.status_code == 400
    assert SocialMediaLink.objects.count() == config.MAX_SOCIAL_MEDIA_LINKS
    assert not SocialMediaLink.objects.filter(name="One Too Many").exists()


@pytest.mark.django_db
def test_editing_an_existing_link_at_the_cap_still_succeeds(staff_client):
    """The cap only blocks *new* rows -- editing one that already exists
    must never be blocked just because the table happens to be full."""
    links = [
        _make(name=f"Link {i}", order=i) for i in range(config.MAX_SOCIAL_MEDIA_LINKS)
    ]
    target = links[0]

    response = staff_client.post(
        _update_url(target),
        {
            "name": "Renamed",
            "url": target.url,
            "platform": target.platform,
            "icon_color": target.icon_color,
        },
        follow=True,
    )

    assert response.status_code == 200
    target.refresh_from_db()
    assert target.name == "Renamed"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_updating_a_link_changes_its_fields(staff_client):
    link = _make()

    response = staff_client.post(
        _update_url(link),
        {
            "name": "Bancostore Official",
            "url": "https://facebook.com/bancostoreofficial",
            "platform": "facebook",
            "icon_color": "#111111",
        },
        follow=True,
    )

    assert response.status_code == 200
    link.refresh_from_db()
    assert link.name == "Bancostore Official"
    assert link.icon_color == "#111111"


@pytest.mark.django_db
def test_updating_a_link_redirects_to_the_general_settings_tab(staff_client):
    link = _make()

    response = staff_client.post(
        _update_url(link),
        {
            "name": "Renamed",
            "url": link.url,
            "platform": link.platform,
            "icon_color": link.icon_color,
        },
    )

    assert response.status_code == 302
    assert response.url == _general_settings_url()


@pytest.mark.django_db
def test_updating_a_link_with_invalid_data_does_not_change_it(staff_client):
    link = _make()

    response = staff_client.post(
        _update_url(link),
        {
            "name": "",  # required field left blank
            "url": link.url,
            "platform": link.platform,
            "icon_color": link.icon_color,
        },
    )

    assert response.status_code == 400
    link.refresh_from_db()
    assert link.name == "Facebook"


@pytest.mark.django_db
def test_updating_a_link_with_invalid_data_still_renders_platform_settings(
    staff_client,
):
    """The failure path must re-render the *full* platform_settings page
    (constance form + groups + social links), not a page that no longer
    exists."""
    link = _make()

    response = staff_client.post(
        _update_url(link),
        {
            "name": "",
            "url": link.url,
            "platform": link.platform,
            "icon_color": link.icon_color,
        },
    )

    assert response.status_code == 400
    assert b"Platform Settings" in response.content


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_a_link_removes_it(staff_client):
    link = _make()

    response = staff_client.post(_delete_url(link), follow=True)

    assert response.status_code == 200
    assert not SocialMediaLink.objects.filter(pk=link.pk).exists()


@pytest.mark.django_db
def test_deleting_a_link_redirects_to_the_general_settings_tab(staff_client):
    link = _make()

    response = staff_client.post(_delete_url(link))

    assert response.status_code == 302
    assert response.url == _general_settings_url()


@pytest.mark.django_db
def test_a_non_staff_user_cannot_delete_a_link(client, db):
    link = _make()
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    response = client.post(_delete_url(link))

    assert response.status_code == 403
    assert SocialMediaLink.objects.filter(pk=link.pk).exists()


# ---------------------------------------------------------------------------
# Detect-platform endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_detect_platform_returns_the_matched_slug(staff_client):
    response = staff_client.get(
        _detect_url(), {"url": "https://instagram.com/bancostore"}
    )

    assert response.status_code == 200
    assert response.json() == {"platform": "instagram"}


@pytest.mark.django_db
def test_detect_platform_returns_custom_for_an_unrecognized_url(staff_client):
    response = staff_client.get(_detect_url(), {"url": "https://example.com"})

    assert response.status_code == 200
    assert response.json() == {"platform": "custom"}


@pytest.mark.django_db
def test_detect_platform_never_echoes_the_raw_url_in_the_response(staff_client):
    """doubt-driven-development finding: the candidate URL must never be
    reflected back into the response body, closing off any reflected-
    content path even though this is a JSON, not HTML, response."""
    response = staff_client.get(
        _detect_url(),
        {"url": "https://example.com/<script>alert(1)</script>"},
    )

    assert response.status_code == 200
    assert b"<script>" not in response.content


# ---------------------------------------------------------------------------
# Icon picker (Task 36d)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_settings_page_never_clips_the_icon_picker_dropdown(staff_client):
    """Task 36d: the row-list and add-form wrappers used to carry
    overflow-hidden, clipping the picker dropdown's own absolutely-
    positioned option list after ~2 items -- both wrapper divs must no
    longer carry it."""
    _make()

    response = staff_client.get(_settings_url())

    content = response.content.decode()
    row_list_wrapper = (
        'class="bg-surface-container-lowest rounded-xl border '
        'border-outline-variant divide-y divide-outline-variant">'
    )
    add_form_wrapper = (
        'bg-surface-container-lowest rounded-xl border border-outline-variant" '
        'x-data="socialLinkPickerState'
    )
    assert row_list_wrapper in content
    assert add_form_wrapper in content


@pytest.mark.django_db
def test_settings_page_includes_default_color_lookup_for_every_curated_icon(
    staff_client,
):
    """Task 36d: selecting an icon should auto-apply its real brand color
    -- the lookup table this relies on must actually be present and cover
    every curated platform, not just some."""
    from apps.pages.social_icons import SOCIAL_ICONS

    response = staff_client.get(_settings_url())

    content = response.content.decode()
    assert "SOCIAL_ICON_DEFAULT_COLORS" in content
    for icon in SOCIAL_ICONS.values():
        assert f'"{icon.slug}": "{icon.default_color}"' in content


@pytest.mark.django_db
def test_settings_page_has_a_reset_to_default_color_control(staff_client):
    response = staff_client.get(_settings_url())

    assert b"resetColorToDefault" in response.content
    assert b"Reset to this platform" in response.content


# ---------------------------------------------------------------------------
# Footer rendering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_footer_renders_configured_social_links(client, db):
    _make(name="Bancostore Facebook", icon_color="#0866FF")

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "https://facebook.com/bancostore" in content
    assert "color: #0866FF" in content


@pytest.mark.django_db
def test_footer_opens_social_links_in_a_new_tab_safely(client, db):
    _make()

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert 'target="_blank"' in content
    assert 'rel="noopener noreferrer"' in content


@pytest.mark.django_db
def test_footer_falls_back_to_placeholders_when_no_links_are_configured(client, db):
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "Coming soon" in content


@pytest.mark.django_db
def test_footer_html_escapes_the_link_name_even_if_it_bypasses_form_validation(
    client, db
):
    """`name` has no format validator (only max_length), unlike
    url/icon_color/platform -- if a future bulk-import script ever
    bypasses SocialMediaLinkForm and calls .save() directly with an
    HTML-ish name, Django's template auto-escaping (project-wide default,
    never overridden for this field) must still render it as inert text,
    not markup."""
    SocialMediaLink.objects.create(
        name="<script>alert(1)</script>",
        url="https://facebook.com/x",
        platform="custom",
        icon_color="#FFFFFF",
        order=1,
    )

    response = client.get(reverse("catalog:home"))

    assert response.status_code == 200
    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;" in response.content


@pytest.mark.django_db
def test_footer_layout_is_four_columns_with_follow_us_below(client, db):
    """Task 36b: Brand/Shop/Company/Policies as 4 equal columns, Follow Us
    as its own row below -- not a 5th column."""
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "lg:grid-cols-4" in content
    assert "lg:grid-cols-5" not in content
