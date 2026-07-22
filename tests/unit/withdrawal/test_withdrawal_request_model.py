from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

import pytest

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_fields_round_trip_correctly():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    reloaded = WithdrawalRequest.objects.get(pk=request.pk)
    assert reloaded.distributor_id == distributor.pk
    assert reloaded.amount == Decimal("500.00")
    assert reloaded.tax_amount == Decimal("5.00")
    assert reloaded.net_amount == Decimal("495.00")


@pytest.mark.django_db
def test_status_defaults_to_submitted():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    assert request.status == WithdrawalRequest.Status.SUBMITTED


@pytest.mark.django_db
def test_payout_destination_snapshot_fields_are_blank_until_approval():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    assert request.payout_mobile_money_number == ""
    assert request.payout_mobile_money_network == ""


@pytest.mark.django_db
def test_review_and_paystack_fields_default_empty():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    assert request.reviewed_by is None
    assert request.reviewed_at is None
    assert request.rejection_reason == ""
    assert request.paystack_recipient_code in ("", None)
    assert request.paystack_transfer_reference is None


@pytest.mark.django_db
def test_two_requests_can_both_have_no_paystack_transfer_reference():
    """paystack_transfer_reference is unique when set (it's the pinned
    idempotency reference, ADR-0004 point 6) but must allow multiple NULLs
    -- mirrors Distributor.starter_pack_payment_reference's existing
    null=True/unique=True convention, since a plain empty-string unique
    field would collide on the second unset row."""
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )
    second = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("200.00"),
        tax_amount=Decimal("2.00"),
        net_amount=Decimal("198.00"),
    )

    assert second.pk is not None


@pytest.mark.django_db
def test_rejects_negative_amount_at_db_level():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WithdrawalRequest.objects.create(
                distributor=distributor,
                amount=Decimal("-500.00"),
                tax_amount=Decimal("5.00"),
                net_amount=Decimal("495.00"),
            )


@pytest.mark.django_db
def test_rejects_negative_tax_amount_at_db_level():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WithdrawalRequest.objects.create(
                distributor=distributor,
                amount=Decimal("500.00"),
                tax_amount=Decimal("-5.00"),
                net_amount=Decimal("505.00"),
            )


@pytest.mark.django_db
def test_rejects_negative_net_amount_at_db_level():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WithdrawalRequest.objects.create(
                distributor=distributor,
                amount=Decimal("500.00"),
                tax_amount=Decimal("600.00"),
                net_amount=Decimal("-100.00"),
            )


@pytest.mark.django_db
def test_rejects_net_amount_greater_than_amount_at_db_level():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WithdrawalRequest.objects.create(
                distributor=distributor,
                amount=Decimal("500.00"),
                tax_amount=Decimal("5.00"),
                net_amount=Decimal("600.00"),
            )


@pytest.mark.django_db
def test_history_tracks_status_transitions():
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor()
    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    request.status = WithdrawalRequest.Status.REJECTED
    request.rejection_reason = "Invalid mobile money number"
    request.save()

    history = request.history.all().order_by("history_date")
    assert history.count() == 2
    assert history.first().status == WithdrawalRequest.Status.SUBMITTED
    assert history.last().status == WithdrawalRequest.Status.REJECTED
