from decimal import Decimal
from itertools import count

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_phone_seq = count(1)


def _make_eligible_distributor(balance=Decimal("1000.00"), full_name="Ama Mensah"):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name=full_name,
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    if balance > 0:
        credit(
            distributor,
            balance,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference=f"seed-{distributor.pk}",
        )
    return distributor


def _make_submitted_request(distributor, amount=Decimal("500.00")):
    from apps.withdrawal.services import submit_withdrawal_request

    return submit_withdrawal_request(distributor, amount)


def _changelist_url():
    return reverse("admin:withdrawal_withdrawalrequest_changelist")


@pytest.mark.django_db
def test_staff_can_approve_via_the_bulk_admin_action(staff_client):
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor, Decimal("500.00"))

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "approve_selected",
            ACTION_CHECKBOX_NAME: [str(request.pk)],
        },
        follow=True,
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.APPROVED_DEBITED
    assert request.payout_mobile_money_number == "+233247111222"
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("505.00")


@pytest.mark.django_db
def test_reject_action_first_shows_a_confirmation_page_asking_for_a_reason(
    staff_client,
):
    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected",
            ACTION_CHECKBOX_NAME: [str(request.pk)],
        },
    )

    assert response.status_code == 200
    assert b"reason" in response.content.lower()
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.SUBMITTED


@pytest.mark.django_db
def test_reject_action_with_a_reason_rejects_and_never_debits(staff_client):
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected",
            ACTION_CHECKBOX_NAME: [str(request.pk)],
            "reason": "Invalid mobile money number",
        },
        follow=True,
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.REJECTED
    assert request.rejection_reason == "Invalid mobile money number"
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_reject_action_rejects_an_empty_reason(staff_client):
    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected",
            ACTION_CHECKBOX_NAME: [str(request.pk)],
            "reason": "   ",
        },
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.SUBMITTED


@pytest.mark.django_db
def test_bulk_approve_processes_every_valid_row_even_if_one_row_fails(staff_client):
    """One request already approved (simulating another admin having just
    handled it), one still genuinely pending -- the pending one must still
    get approved, not blocked by the other row's failure."""
    already_handled_distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    already_handled = _make_submitted_request(
        already_handled_distributor, Decimal("500.00")
    )
    already_handled.status = WithdrawalRequest.Status.APPROVED_DEBITED
    already_handled.save()

    pending_distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    pending = _make_submitted_request(pending_distributor, Decimal("500.00"))

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "approve_selected",
            ACTION_CHECKBOX_NAME: [str(already_handled.pk), str(pending.pk)],
        },
        follow=True,
    )

    assert response.status_code == 200
    pending.refresh_from_db()
    assert pending.status == WithdrawalRequest.Status.APPROVED_DEBITED
    wallet = Wallet.objects.get(distributor=pending_distributor)
    assert wallet.balance == Decimal("505.00")


@pytest.mark.django_db
def test_bulk_approve_does_not_crash_on_a_kyc_revoked_row(staff_client):
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    distributor.kyc_status = Distributor.KycStatus.PENDING
    distributor.save()

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "approve_selected",
            ACTION_CHECKBOX_NAME: [str(request.pk)],
        },
        follow=True,
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.SUBMITTED
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_bulk_approve_isolates_a_row_whose_record_vanished_mid_batch(
    staff_client, monkeypatch
):
    """code-review-and-quality (2026-07-23): WithdrawalRequestNotFound is
    documented as a real exception approve_withdrawal_request can raise
    (e.g. the row is deleted by an out-of-band process between the
    changelist selection and this row's turn in the bulk loop), but the
    admin action never caught it -- it would propagate out of the for
    loop, 500ing the *entire* bulk action and losing the results summary
    for every row already processed in the same request, even though
    each row's own approval is independently atomic and already
    committed. Simulates the "vanished mid-batch" case directly via
    monkeypatch rather than fighting Django queryset caching to force a
    genuine race -- what's under test here is the admin loop's own
    error-isolation, not the service layer's locking (already proven by
    test_concurrent_approvals_of_the_same_request_never_both_debit)."""
    import apps.withdrawal.admin as withdrawal_admin

    vanished_distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    vanished = _make_submitted_request(vanished_distributor, Decimal("500.00"))
    pending_distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    pending = _make_submitted_request(pending_distributor, Decimal("500.00"))

    real_approve = withdrawal_admin.approve_withdrawal_request

    def fake_approve(withdrawal_request, *, reviewed_by):
        if withdrawal_request.pk == vanished.pk:
            raise withdrawal_admin.WithdrawalRequestNotFound(
                f"WithdrawalRequest {withdrawal_request.pk} does not exist"
            )
        return real_approve(withdrawal_request, reviewed_by=reviewed_by)

    monkeypatch.setattr(withdrawal_admin, "approve_withdrawal_request", fake_approve)

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "approve_selected",
            ACTION_CHECKBOX_NAME: [str(vanished.pk), str(pending.pk)],
        },
        follow=True,
    )

    assert response.status_code == 200
    pending.refresh_from_db()
    assert pending.status == WithdrawalRequest.Status.APPROVED_DEBITED
    wallet = Wallet.objects.get(distributor=pending_distributor)
    assert wallet.balance == Decimal("505.00")
