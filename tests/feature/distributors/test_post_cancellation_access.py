from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest
from constance import config

from apps.distributors.cooling_off_services import cancel_membership_and_refund
from apps.distributors.models import Distributor
from apps.notifications.models import Notification

User = get_user_model()


def _make_distributor(phone="+233248100001"):
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(
        user=user, phone_number=phone, phone_verified=True
    )


def _confirm_pack_b_purchase(distributor, reference="pack-ref-1"):
    from apps.distributors.services import consume_paid_starter_pack

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


def _cancelled_distributor(phone="+233248100001"):
    """A distributor who bought a starter pack, then exercised their
    cooling-off cancellation -- the exact state that left them unable to
    reach their own credited refund before this fix."""
    distributor = _confirm_pack_b_purchase(_make_distributor(phone))
    cancel_membership_and_refund(distributor.pk)
    distributor.refresh_from_db()
    return distributor


@pytest.mark.django_db
def test_a_cooling_off_cancelled_distributor_can_still_log_in(client):
    """Regression test: cancel_membership_and_refund sets user.is_active
    = False, which blocks Django's default authentication entirely --
    but the whole point of crediting the refund to the distributor's own
    wallet (ADR-0007 Decision 7) is that they have a real path to
    actually receive it. Without this fix, that promise is false: the
    refund is credited but permanently unreachable."""
    distributor = _cancelled_distributor()

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": str(distributor.phone_number), "password": "Passw0rd!"},
    )

    assert response.status_code == 302
    assert response.url == reverse("distributors:dashboard")


@pytest.mark.django_db
def test_a_regularly_deactivated_distributor_still_cannot_log_in(client):
    """The login carve-out must be narrowly scoped to cooling-off
    cancellation -- an admin's unrelated suspend toggle (user.is_active
    = False with no cooling_off_cancelled_at) must keep blocking login
    exactly as before."""
    distributor = _confirm_pack_b_purchase(_make_distributor())
    distributor.user.is_active = False
    distributor.user.save(update_fields=["is_active"])

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": str(distributor.phone_number), "password": "Passw0rd!"},
    )

    assert response.status_code == 200  # re-renders the login form
    assert "Incorrect phone number or password" in response.content.decode()


@pytest.mark.django_db
def test_dashboard_redirects_a_cancelled_distributor_to_withdrawal_request(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:withdrawal_request")


@pytest.mark.django_db
def test_earnings_history_redirects_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:earnings_history"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:withdrawal_request")


@pytest.mark.django_db
def test_select_starter_pack_redirects_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:select_starter_pack"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:withdrawal_request")


@pytest.mark.django_db
def test_start_kyc_verification_redirects_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:start_kyc_verification"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:withdrawal_request")


@pytest.mark.django_db
def test_withdrawal_request_stays_reachable_for_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:withdrawal_request"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "GHS 1,800.00" in body or "1800.00" in body
    assert "Your membership was cancelled" in body


@pytest.mark.django_db
def test_withdrawal_request_has_no_cancellation_banner_for_a_regular_distributor(
    client,
):
    distributor = _confirm_pack_b_purchase(_make_distributor())
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:withdrawal_request"))

    assert "Your membership was cancelled" not in response.content.decode()


@pytest.mark.django_db
def test_withdrawal_history_stays_reachable_for_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:withdrawal_history"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_payout_settings_stays_reachable_for_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:payout_settings"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_notification_dropdown_stays_reachable_for_a_cancelled_distributor(client):
    """Task 21d-iv, caught by a code-review pass before merge:
    notification_dropdown is only ever called via htmx.ajax() GET
    targeting a small #notif-panel-content div -- a bare page redirect
    (which @_redirect_if_cooling_off_cancelled would have issued here)
    gets followed transparently by that GET and swaps whole-page content
    into the tiny dropdown. A cancelled distributor's own notification
    history (which can include a still-relevant WITHDRAWAL_APPROVED
    notification for the refund they're claiming) is also never
    "nothing left to see" the way starter-pack/team pages are."""
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:notification_dropdown"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_notification_history_stays_reachable_for_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.get(reverse("distributors:notification_history"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_notification_mark_read_stays_reachable_for_a_cancelled_distributor(client):
    distributor = _cancelled_distributor()
    notification = Notification.objects.create(
        distributor=distributor,
        event_type=Notification.EventType.WITHDRAWAL_APPROVED,
        message="Your withdrawal was approved",
        is_read=False,
    )
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.post(
        reverse("distributors:notification_mark_read", args=[notification.pk])
    )

    assert response.status_code == 200
    notification.refresh_from_db()
    assert notification.is_read is True


@pytest.mark.django_db
def test_notification_mark_all_read_stays_reachable_for_a_cancelled_distributor(
    client,
):
    distributor = _cancelled_distributor()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.post(reverse("distributors:notification_mark_all_read"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_a_cancelled_distributor_can_actually_submit_a_withdrawal(client):
    """End-to-end proof: the credited refund is genuinely claimable, not
    just visible."""
    distributor = _cancelled_distributor()
    distributor.mobile_money_number = "+233248100099"
    distributor.mobile_money_network = "mtn"
    distributor.kyc_status = Distributor.KycStatus.APPROVED
    distributor.save()
    client.login(phone_number=str(distributor.phone_number), password="Passw0rd!")

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "1800.00"}
    )

    assert response.status_code == 302
    from apps.withdrawal.models import WithdrawalRequest

    assert WithdrawalRequest.objects.filter(distributor=distributor).exists()
