import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

import pytest


def _signed_post(client, payload):
    body = json.dumps(payload).encode()
    signature = hmac.new(b"sk_test_fake", body, hashlib.sha512).hexdigest()
    return client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_success_dispatches_the_reference_to_the_deferred_task(
    mock_task, client
):
    response = _signed_post(
        client,
        {
            "event": "transfer.success",
            "data": {"reference": "withdrawal-payout-0000000042"},
        },
    )

    assert response.status_code == 200
    mock_task.delay.assert_called_once_with("withdrawal-payout-0000000042")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_failed_dispatches_the_reference_to_the_deferred_task(
    mock_task, client
):
    response = _signed_post(
        client,
        {
            "event": "transfer.failed",
            "data": {"reference": "withdrawal-payout-0000000042"},
        },
    )

    assert response.status_code == 200
    mock_task.delay.assert_called_once_with("withdrawal-payout-0000000042")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_reversed_dispatches_the_reference_to_the_deferred_task(
    mock_task, client
):
    response = _signed_post(
        client,
        {
            "event": "transfer.reversed",
            "data": {"reference": "withdrawal-payout-0000000042"},
        },
    )

    assert response.status_code == 200
    mock_task.delay.assert_called_once_with("withdrawal-payout-0000000042")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_event_with_an_unrecognized_reference_is_not_dispatched(
    mock_task, client
):
    response = _signed_post(
        client,
        {"event": "transfer.success", "data": {"reference": "something-else"}},
    )

    assert response.status_code == 200
    mock_task.delay.assert_not_called()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_event_with_a_missing_reference_is_not_dispatched(mock_task, client):
    """doubt-driven-development finding, 2026-07-24: a blank reference
    (e.g. an unexpected payload shape) must be handled the same as an
    unrecognized prefix -- not fall through silently with no log line at
    all, which is worse than the unrecognized-prefix case."""
    response = _signed_post(client, {"event": "transfer.success", "data": {}})

    assert response.status_code == 200
    mock_task.delay.assert_not_called()


@override_settings(PAYSTACK_SECRET_KEY="sk_test_fake")
@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_an_unrelated_paystack_event_does_not_trigger_the_transfer_task(
    mock_task, client
):
    response = _signed_post(
        client,
        {
            "event": "charge.success",
            "data": {"reference": "withdrawal-payout-0000000042"},
        },
    )

    assert response.status_code == 200
    mock_task.delay.assert_not_called()


@pytest.mark.django_db
@patch("apps.distributors.views.process_transfer_webhook_task")
def test_transfer_webhook_rejects_an_invalid_signature(mock_task, client):
    body = json.dumps(
        {"event": "transfer.success", "data": {"reference": "withdrawal-payout-1"}}
    ).encode()

    response = client.post(
        reverse("distributors:paystack_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE="not-a-real-signature",
    )

    assert response.status_code == 400
    mock_task.delay.assert_not_called()
