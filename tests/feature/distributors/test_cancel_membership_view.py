from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

import pytest
from constance import config

from apps.distributors.models import Distributor

User = get_user_model()


def _make_distributor(phone="+233248000001"):
    """Mirrors tests/feature/distributors/test_payout_settings.py's own
    convention -- see that file for the same helper shape."""
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


def _confirm_pack_b_purchase(distributor, sponsor=None, reference="pack-ref-1"):
    """Runs the real consume_paid_starter_pack path (mirrors
    tests/unit/distributors/test_cooling_off_refund.py's own convention)
    so this view is tested against genuinely-credited state."""
    from apps.distributors.services import consume_paid_starter_pack

    distributor.sponsor = sponsor
    distributor.starter_pack_choice = "B"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_B_PV
    distributor.starter_pack_rank = config.STARTER_PACK_B_RANK
    distributor.starter_pack_payment_reference = reference
    distributor.save()

    with patch("apps.distributors.services.verify_transaction") as mock_verify:
        mock_verify.return_value = {
            "status": "success",
            "amount": distributor.starter_pack_price_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack(reference)
    distributor.refresh_from_db()
    return distributor


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_shows_eligible_state_with_refund_breakdown_within_the_window(client):
    distributor = _confirm_pack_b_purchase(_make_distributor())
    _login(client, distributor)

    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Eligible for Refund" in body
    assert "GHS 2,000.00" in body or "GHS 2000.00" in body
    assert "GHS 1,800.00" in body or "GHS 1800.00" in body
    assert "Cancel Membership" in body


@pytest.mark.django_db
def test_shows_ineligible_state_when_no_starter_pack_purchase(client):
    distributor = _make_distributor()  # never confirmed a purchase
    _login(client, distributor)

    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 200
    assert "Cooling-Off Period Ended" in response.content.decode()


@pytest.mark.django_db
def test_shows_ineligible_state_after_the_window_has_expired(client):
    distributor = _confirm_pack_b_purchase(_make_distributor())
    distributor.starter_pack_confirmed_at = timezone.now() - timedelta(days=8)
    distributor.save(update_fields=["starter_pack_confirmed_at"])
    _login(client, distributor)

    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Cooling-Off Period Ended" in body
    assert "Eligible for Refund" not in body


@pytest.mark.django_db
def test_post_cancels_membership_credits_wallet_and_logs_out(client):
    distributor = _confirm_pack_b_purchase(_make_distributor())
    _login(client, distributor)

    response = client.post(reverse("distributors:cancel_membership"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Membership Cancelled" in body
    assert "GHS 1,800.00" in body or "GHS 1800.00" in body
    distributor.refresh_from_db()
    assert distributor.user.is_active is False
    assert distributor.cooling_off_cancelled_at is not None

    # The session was logged out server-side -- a subsequent
    # authenticated request must be redirected to login again.
    dashboard_response = client.get(reverse("distributors:dashboard"))
    assert dashboard_response.status_code == 302
    assert dashboard_response.url.startswith(reverse("distributors:login"))


def test_membership_cancelled_page_distinguishes_zero_refund_from_no_refund():
    """Regression test (code-review-and-quality, post-implementation):
    templates/distributors/membership_cancelled.html originally used
    `{% if refund_amount %}`, a truthy check -- but Decimal("0.00") is
    falsy in Python/Django templates, so a genuine (if admin-
    misconfiguration-only-reachable) GHS 0.00 refund would have shown
    "Your membership was already cancelled" instead of the real GHS 0.00
    confirmation. Fixed to `is not None`, which correctly distinguishes
    "no refund happened" (None, the idempotent-no-op return value) from
    "a real refund of exactly zero happened" (Decimal("0.00"))."""
    from django.template.loader import render_to_string

    zero_refund_body = render_to_string(
        "distributors/membership_cancelled.html", {"refund_amount": Decimal("0.00")}
    )
    assert "GHS 0.00" in zero_refund_body
    assert "already cancelled" not in zero_refund_body

    no_op_body = render_to_string(
        "distributors/membership_cancelled.html", {"refund_amount": None}
    )
    assert "already cancelled" in no_op_body


@pytest.mark.django_db
def test_post_credits_the_sponsors_wallet_reversal_too(client):
    sponsor = _make_distributor(phone="+233248000002")
    distributor = _confirm_pack_b_purchase(_make_distributor(), sponsor=sponsor)
    _login(client, distributor)

    client.post(reverse("distributors:cancel_membership"))

    from apps.wallet.models import Wallet

    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    assert sponsor_wallet.balance == Decimal("0.00")


@pytest.mark.django_db
def test_get_after_already_cancelled_shows_ineligible_not_a_crash(client):
    distributor = _confirm_pack_b_purchase(_make_distributor())
    distributor.cooling_off_cancelled_at = timezone.now()
    distributor.save(update_fields=["cooling_off_cancelled_at"])
    # Simulate an admin reactivation so the distributor can still log in
    # and reach this GET -- the view itself, not just login, must treat
    # them as ineligible.
    distributor.user.is_active = True
    distributor.user.save(update_fields=["is_active"])
    _login(client, distributor)

    response = client.get(reverse("distributors:cancel_membership"))

    assert response.status_code == 200
    assert "Cooling-Off Period Ended" in response.content.decode()


@pytest.mark.django_db
def test_never_shows_or_cancels_another_distributors_membership(client):
    own = _confirm_pack_b_purchase(
        _make_distributor(phone="+233248000003"), reference="pack-ref-own"
    )
    other = _confirm_pack_b_purchase(
        _make_distributor(phone="+233248000004"), reference="pack-ref-other"
    )
    _login(client, own)

    client.post(reverse("distributors:cancel_membership"))

    other.refresh_from_db()
    assert other.cooling_off_cancelled_at is None
    assert other.user.is_active is True
