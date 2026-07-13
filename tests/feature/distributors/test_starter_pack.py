import hashlib
import hmac
import json
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

import pytest
from constance import config

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233241{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_select_starter_pack_requires_login(client):
    response = client.get(reverse("distributors:select_starter_pack"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_select_starter_pack_get_shows_pack_options_from_settings(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:select_starter_pack"))

    assert response.status_code == 200
    assert response.context["pack_a_price"] == config.STARTER_PACK_A_PRICE
    assert response.context["pack_a_pv"] == config.STARTER_PACK_A_PV
    assert response.context["pack_b_price"] == config.STARTER_PACK_B_PRICE
    assert response.context["pack_b_pv"] == config.STARTER_PACK_B_PV


@pytest.mark.django_db
@patch("apps.distributors.views.initialize_transaction")
def test_select_starter_pack_post_initializes_and_redirects(mock_init, client):
    distributor = _make_distributor()
    _login(client, distributor)
    mock_init.return_value = {
        "authorization_url": "https://checkout.paystack.com/pack-xyz",
        "access_code": "xyz",
        "reference": "whatever",
    }

    response = client.post(reverse("distributors:select_starter_pack"), {"pack": "B"})

    assert response.status_code == 302
    assert response.url == "https://checkout.paystack.com/pack-xyz"

    distributor.refresh_from_db()
    assert distributor.starter_pack_choice == "B"
    assert distributor.starter_pack_price_pesewas == int(
        config.STARTER_PACK_B_PRICE * 100
    )
    assert distributor.starter_pack_payment_reference.startswith("pack-")

    call_kwargs = mock_init.call_args.kwargs
    assert call_kwargs["reference"] == distributor.starter_pack_payment_reference
    assert call_kwargs["amount_pesewas"] == distributor.starter_pack_price_pesewas


@pytest.mark.django_db
def test_select_starter_pack_rejects_an_invalid_choice(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(reverse("distributors:select_starter_pack"), {"pack": "Z"})

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.starter_pack_choice == ""


@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_starter_pack")
def test_starter_pack_callback_consumes_the_reference(mock_consume, client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(
        reverse("distributors:starter_pack_payment_callback"),
        {"reference": "pack-ref-xyz"},
    )

    assert response.status_code == 200
    mock_consume.assert_called_once_with("pack-ref-xyz")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_starter_pack")
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_dispatches_a_pack_reference_to_consume_paid_starter_pack(
    mock_consume_reg, mock_consume_pack, client
):
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "pack-42-abcd1234"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    mock_consume_pack.assert_called_once_with("pack-42-abcd1234")
    mock_consume_reg.assert_not_called()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_starter_pack")
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_still_dispatches_a_reg_reference_to_consume_paid_registration(
    mock_consume_reg, mock_consume_pack, client
):
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "reg-abc123-9f8e7d6c"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    mock_consume_reg.assert_called_once_with("reg-abc123-9f8e7d6c")
    mock_consume_pack.assert_not_called()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.consume_paid_starter_pack")
@patch("apps.distributors.views.consume_paid_registration")
def test_webhook_ignores_an_unrecognized_reference_prefix(
    mock_consume_reg, mock_consume_pack, client
):
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "something-else"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    mock_consume_reg.assert_not_called()
    mock_consume_pack.assert_not_called()
