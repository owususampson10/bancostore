from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from constance import config

from apps.pages.models import SocialMediaLink

User = get_user_model()


def _settings_url():
    return reverse("admin_portal:social_links_settings")


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
