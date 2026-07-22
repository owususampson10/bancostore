from decimal import Decimal

from django.forms.widgets import CheckboxInput
from django.urls import reverse

import pytest
from constance import config as live_config
from constance.admin import ConstanceForm

from apps.platform_settings.config import CONSTANCE_CONFIG


def _full_valid_payload(overrides=None):
    """Builds a complete, currently-valid POST payload for the Constance
    admin form -- every one of its ~76 settings is required on every
    submit (constance.admin.ConstanceForm has no partial-update concept),
    so this mirrors what a real browser submitting the real form would
    send: current live values for everything, serialized through each
    field's own widget the same way ConstanceForm itself would, with
    `overrides` layered on top for the specific setting(s) under test."""
    initial = {key: getattr(live_config, key) for key in CONSTANCE_CONFIG}
    form = ConstanceForm(initial=initial, request=None)
    data = {"version": form.initial["version"]}
    for name, field in form.fields.items():
        if name == "version":
            continue
        value = initial.get(name)
        if isinstance(field.widget, CheckboxInput):
            if value:
                data[name] = "on"
            continue
        data[name] = field.prepare_value(value)
    if overrides:
        data.update(overrides)
    return data


@pytest.mark.django_db
def test_staff_cannot_save_min_withdrawal_amount_above_max(staff_client):
    """CodeRabbit review, 2026-07-22: real end-to-end proof (not just the
    isolated validation function) that the actual admin save path rejects
    an inverted MIN/MAX_WITHDRAWAL_AMOUNT pair."""
    payload = _full_valid_payload(
        {
            "MIN_WITHDRAWAL_AMOUNT": "20000",
            "MAX_WITHDRAWAL_AMOUNT": "10000",
        }
    )

    response = staff_client.post(reverse("admin:constance_config_changelist"), payload)

    assert response.status_code == 200  # re-renders the form with an error, no redirect
    assert b"cannot be greater than" in response.content
    assert live_config.MIN_WITHDRAWAL_AMOUNT == Decimal("100")
    assert live_config.MAX_WITHDRAWAL_AMOUNT == Decimal("10000")


@pytest.mark.django_db
def test_staff_can_save_a_valid_min_max_withdrawal_amount_pair(staff_client):
    payload = _full_valid_payload(
        {
            "MIN_WITHDRAWAL_AMOUNT": "150",
            "MAX_WITHDRAWAL_AMOUNT": "8000",
        }
    )

    response = staff_client.post(reverse("admin:constance_config_changelist"), payload)

    assert response.status_code == 302
    assert live_config.MIN_WITHDRAWAL_AMOUNT == Decimal("150")
    assert live_config.MAX_WITHDRAWAL_AMOUNT == Decimal("8000")
