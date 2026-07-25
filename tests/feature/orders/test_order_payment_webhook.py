import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

import pytest


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.confirm_order_payment")
def test_webhook_dispatches_an_order_reference_to_confirm_order_payment(
    mock_confirm, client
):
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": "order-abc123"}}
    ).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )

    assert response.status_code == 200
    mock_confirm.assert_called_once_with("order-abc123")
