from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.html import escape

import pytest
from constance import config
from constance.utils import get_values

from apps.platform_settings.admin import BancostoreConstanceForm
from apps.platform_settings.config import CONSTANCE_CONFIG, CONSTANCE_CONFIG_FIELDSETS

User = get_user_model()


def _platform_settings_url():
    return reverse("admin_portal:platform_settings")


def _valid_post_data(overrides=None):
    """Builds a complete, valid payload for every constance-backed field --
    IntegerField/DecimalField/the CONSTANCE_ADDITIONAL_FIELDS custom fields
    are all required (only BooleanField and the plain str CharField default
    to required=False in constance's own FIELDS mapping), so a real save
    always submits every setting together, not just one group's worth."""
    data = {}
    for name, options in CONSTANCE_CONFIG.items():
        default = options[0]
        if isinstance(default, bool):
            if default:
                data[name] = "on"
            continue
        data[name] = str(default)
    if overrides:
        data.update(overrides)
    # The hidden version field is a hash of the *current* stored values,
    # computed by BancostoreConstanceForm itself -- not something a test
    # should reimplement, since that would just duplicate (and risk
    # drifting from) the real hashing logic under test.
    data["version"] = BancostoreConstanceForm(initial=get_values()).initial["version"]
    return data


@pytest.mark.django_db
def test_staff_can_reach_platform_settings_page(staff_client):
    response = staff_client.get(_platform_settings_url())

    assert response.status_code == 200
    assert b"Platform Settings" in response.content


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    user = User.objects.create_user(
        username="+233249999999", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_platform_settings_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_platform_settings_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url


@pytest.mark.django_db
def test_page_shows_every_configuration_group(staff_client):
    response = staff_client.get(_platform_settings_url())

    body = response.content.decode()
    for group_title in CONSTANCE_CONFIG_FIELDSETS:
        # Django auto-escapes "&" to "&amp;" in rendered HTML (e.g.
        # "Commission & Bonus Settings") -- escape() matches that, rather
        # than the test asserting on raw unescaped text that will never
        # literally appear in the response.
        assert escape(group_title) in body


@pytest.mark.django_db
def test_page_shows_a_real_setting_and_its_current_value(staff_client):
    response = staff_client.get(_platform_settings_url())

    body = response.content.decode()
    assert "OTP_CODE_EXPIRY_MINUTES" in body or "OTP code expiry" in body.lower()
    assert 'value="15"' in body


@pytest.mark.django_db
def test_setting_names_are_shown_as_human_readable_labels(staff_client):
    """Raw constant names like DISTRIBUTOR_LOGIN_METHOD read as code, not a
    setting a non-technical admin can recognize -- the visible label must
    be spaced-out words, with known acronyms (OTP/KYC/2FA/IR ID/PV) cased
    correctly rather than a bare str.title() mangling them into "Otp"/
    "Kyc"/"2Fa"/"Ir Id"/"Pv"."""
    response = staff_client.get(_platform_settings_url())

    body = response.content.decode()
    assert "Distributor Login Method" in body
    assert "KYC Required" in body
    assert "Admin 2FA Method" in body
    assert "IR ID Prefix" in body
    assert "Min Monthly Personal PV" in body


@pytest.mark.django_db
def test_saving_updates_a_boolean_constance_setting(staff_client):
    assert config.MAINTENANCE_MODE_ENABLED is False

    response = staff_client.post(
        _platform_settings_url(),
        _valid_post_data({"MAINTENANCE_MODE_ENABLED": "on"}),
    )

    assert response.status_code == 302
    assert config.MAINTENANCE_MODE_ENABLED is True


@pytest.mark.django_db
def test_admin_2fa_enabled_survives_a_save_even_when_the_client_omits_it(
    staff_client,
):
    """A real disabled checkbox is never included in a browser's own form
    submission -- simulated here by deleting the key entirely, not just
    setting it False, since that's what an actual client sends. The field
    must still come out True afterward (its real, unchanged stored value),
    proving the disabled-field protection actually works end to end and
    not just "looks locked" in the rendered HTML."""
    assert config.ADMIN_2FA_ENABLED is True

    data = _valid_post_data()
    del data["ADMIN_2FA_ENABLED"]

    response = staff_client.post(_platform_settings_url(), data)

    assert response.status_code == 302
    assert config.ADMIN_2FA_ENABLED is True


@pytest.mark.django_db
def test_saving_updates_a_decimal_constance_setting(staff_client):
    response = staff_client.post(
        _platform_settings_url(),
        _valid_post_data({"BINARY_BONUS_RATE": "8.25"}),
    )

    assert response.status_code == 302
    assert config.BINARY_BONUS_RATE == Decimal("8.25")


@pytest.mark.django_db
def test_saving_shows_a_success_message(staff_client):
    response = staff_client.post(
        _platform_settings_url(), _valid_post_data(), follow=True
    )

    assert b"updated successfully" in response.content


@pytest.mark.django_db
def test_cross_field_withdrawal_amount_validation_still_enforced(staff_client):
    """BancostoreConstanceForm.clean() rejects MIN_WITHDRAWAL_AMOUNT >
    MAX_WITHDRAWAL_AMOUNT -- this page must reuse that same validation,
    not a hand-rolled form that could silently drop it."""
    response = staff_client.post(
        _platform_settings_url(),
        _valid_post_data(
            {
                "MIN_WITHDRAWAL_AMOUNT": "9000",
                "MAX_WITHDRAWAL_AMOUNT": "100",
            }
        ),
    )

    assert response.status_code == 200
    assert config.MIN_WITHDRAWAL_AMOUNT != Decimal("9000")
    assert b"cannot be greater than" in response.content


@pytest.mark.django_db
def test_out_of_range_percentage_field_is_rejected(staff_client):
    """percentage_field bounds BINARY_BONUS_RATE to 0-100 -- a fat-fingered
    750 (the real incident this bound was added to prevent, per
    CONSTANCE_ADDITIONAL_FIELDS's own comment) must not silently save."""
    response = staff_client.post(
        _platform_settings_url(),
        _valid_post_data({"BINARY_BONUS_RATE": "750"}),
    )

    assert response.status_code == 200
    assert config.BINARY_BONUS_RATE != Decimal("750")
