from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.notifications.models import NotificationTemplate
from apps.notifications.sms import fake_outbox
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    apply_verified_transfer_outcome,
    approve_withdrawal_request,
    claim_for_payout,
    reject_withdrawal_request,
    submit_withdrawal_request,
)

User = get_user_model()
_phone_seq = count(1)


def _make_submitted_request(amount=Decimal("500.00")):
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name="Ama Mensah",
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    credit(
        distributor,
        Decimal("1000.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference=f"seed-{distributor.pk}",
    )
    return submit_withdrawal_request(distributor, amount), distributor


def _make_admin():
    return User.objects.create_user(
        username=f"admin-{next(_phone_seq)}", password="Passw0rd!", is_staff=True
    )


def _make_queued_for_payout_request(amount=Decimal("500.00")):
    request, distributor = _make_submitted_request(amount)
    approved = approve_withdrawal_request(request, reviewed_by=_make_admin())
    return claim_for_payout(approved), distributor


@pytest.mark.django_db
def test_approval_notifies_the_distributor_and_names_the_payout_day():
    """Task 16g acceptance criteria: the copy must be honest that
    "approved" does not mean "paid yet"."""
    request, distributor = _make_submitted_request(amount=Decimal("500.00"))

    approve_withdrawal_request(request, reviewed_by=_make_admin())

    assert fake_outbox, "no SMS was sent on approval"
    message = fake_outbox[-1]
    assert message["phone_number"] == str(distributor.phone_number)
    assert "495" in message["message"]  # net amount, 1% tax on 500
    assert "Friday" in message["message"]


@pytest.mark.django_db
def test_rejection_notifies_the_distributor_with_the_reason():
    request, distributor = _make_submitted_request()

    reject_withdrawal_request(
        request, reviewed_by=_make_admin(), reason="KYC re-verification required"
    )

    assert fake_outbox, "no SMS was sent on rejection"
    message = fake_outbox[-1]
    assert message["phone_number"] == str(distributor.phone_number)
    assert "KYC re-verification required" in message["message"]


@pytest.mark.django_db
def test_a_paid_outcome_notifies_the_distributor():
    request, distributor = _make_queued_for_payout_request(amount=Decimal("500.00"))

    apply_verified_transfer_outcome(request, "success")

    assert fake_outbox, "no SMS was sent on payout"
    message = fake_outbox[-1]
    assert message["phone_number"] == str(distributor.phone_number)
    assert "495" in message["message"]
    assert "paid" in message["message"].lower()


@pytest.mark.django_db
def test_a_reversed_outcome_notifies_the_distributor_it_returned_to_wallet():
    request, distributor = _make_queued_for_payout_request(amount=Decimal("500.00"))

    apply_verified_transfer_outcome(request, "failed")

    assert fake_outbox, "no SMS was sent on reversal"
    message = fake_outbox[-1]
    assert message["phone_number"] == str(distributor.phone_number)
    assert "wallet" in message["message"].lower()


@pytest.mark.django_db
def test_a_non_terminal_status_does_not_notify():
    request, _ = _make_queued_for_payout_request()
    fake_outbox.clear()

    apply_verified_transfer_outcome(request, "pending")

    assert not fake_outbox


@pytest.mark.django_db
def test_applying_the_same_paid_outcome_twice_notifies_only_once():
    """doubt-driven-development, 2026-07-24: apply_verified_transfer_
    outcome is idempotent (a duplicate webhook delivery, or the webhook
    racing the batch driver's own resume path, both call it against the
    same request) -- the second call must be a silent no-op, including
    for notifications. Without this, a duplicate delivery would text the
    distributor twice for the same payout."""
    request, _ = _make_queued_for_payout_request()
    fake_outbox.clear()

    apply_verified_transfer_outcome(request, "success")
    apply_verified_transfer_outcome(request, "success")

    assert len(fake_outbox) == 1


@pytest.mark.django_db
def test_applying_the_same_reversal_twice_notifies_only_once():
    request, _ = _make_queued_for_payout_request()
    fake_outbox.clear()

    apply_verified_transfer_outcome(request, "failed")
    apply_verified_transfer_outcome(request, "failed")

    assert len(fake_outbox) == 1


@pytest.mark.django_db
@patch("apps.withdrawal.services.send_sms")
def test_a_failed_sms_send_does_not_undo_the_approval(mock_send_sms):
    """The debit has already committed by the time the SMS is attempted
    -- an SMS provider outage must not roll back real money that was
    correctly debited, mirroring apps.distributors.services's own
    established test for the identical failure mode (Task 12)."""
    request, distributor = _make_submitted_request(amount=Decimal("500.00"))
    mock_send_sms.side_effect = Exception("SMS provider is down")

    approved = approve_withdrawal_request(
        request, reviewed_by=_make_admin()
    )  # must not raise

    assert approved.status == WithdrawalRequest.Status.APPROVED_DEBITED
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("505.00")  # 1000 - 495 net debited


@pytest.mark.django_db
@patch("apps.withdrawal.services.send_sms")
def test_a_failed_sms_send_does_not_undo_a_paid_transition(mock_send_sms):
    request, _ = _make_queued_for_payout_request()
    mock_send_sms.side_effect = Exception("SMS provider is down")

    updated = apply_verified_transfer_outcome(request, "success")  # must not raise

    assert updated.status == WithdrawalRequest.Status.PAID


# ---------------------------------------------------------------------------
# Task 48b acceptance criteria: editing a template's wording from admin_portal
# changes the next real send -- not just that the edit saves. Each key is
# pre-seeded by migration 0006_seed_notification_templates, so these update
# the already-existing row rather than creating a second one.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approval_uses_the_live_admin_edited_template_wording():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.WITHDRAWAL_APPROVED
    ).update(body="Custom: GHS {{net_amount}} approved, pays {{withdrawal_day}}.")
    request, _ = _make_submitted_request(amount=Decimal("500.00"))

    approve_withdrawal_request(request, reviewed_by=_make_admin())

    assert fake_outbox[-1]["message"] == (
        f"Custom: GHS 495.00 approved, pays {config.WITHDRAWAL_DAY.title()}."
    )


@pytest.mark.django_db
def test_rejection_uses_the_live_admin_edited_template_wording():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.WITHDRAWAL_REJECTED
    ).update(body="Custom: GHS {{amount}} rejected -- {{reason}}.")
    request, _ = _make_submitted_request(amount=Decimal("500.00"))

    reject_withdrawal_request(
        request, reviewed_by=_make_admin(), reason="KYC re-verification required"
    )

    assert fake_outbox[-1]["message"] == (
        "Custom: GHS 500.00 rejected -- KYC re-verification required."
    )


@pytest.mark.django_db
def test_a_paid_outcome_uses_the_live_admin_edited_template_wording():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.WITHDRAWAL_PAID
    ).update(body="Custom: GHS {{net_amount}} paid out.")
    request, _ = _make_queued_for_payout_request(amount=Decimal("500.00"))

    apply_verified_transfer_outcome(request, "success")

    assert fake_outbox[-1]["message"] == "Custom: GHS 495.00 paid out."


@pytest.mark.django_db
def test_a_reversed_outcome_uses_the_live_admin_edited_template_wording():
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.WITHDRAWAL_REVERSED
    ).update(body="Custom: GHS {{net_amount}} returned to wallet.")
    request, _ = _make_queued_for_payout_request(amount=Decimal("500.00"))

    apply_verified_transfer_outcome(request, "failed")

    assert fake_outbox[-1]["message"] == "Custom: GHS 495.00 returned to wallet."
