import threading
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from bancostore.concurrency import retry_on_lock_contention

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_credit_creates_a_wallet_and_transaction_for_a_first_time_credit():
    distributor = _make_distributor()

    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="starter-pack-ref-1",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("50.00")

    transaction = WalletTransaction.objects.get(wallet=wallet)
    assert transaction.amount == Decimal("50.00")
    assert (
        transaction.transaction_type
        == WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS
    )
    assert transaction.reference == "starter-pack-ref-1"


@pytest.mark.django_db
def test_a_second_credit_adds_to_the_existing_balance():
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )

    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-2",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("150.00")
    assert WalletTransaction.objects.filter(wallet=wallet).count() == 2


@pytest.mark.django_db
def test_credit_never_uses_a_float_for_the_amount():
    """Guards against a regression where someone passes a float -- money
    must be Decimal everywhere in this path, per CLAUDE.md/SPEC.md."""
    distributor = _make_distributor()

    with pytest.raises(TypeError):
        credit(
            distributor,
            50.0,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="ref-1",
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_credits_never_lose_an_update():
    """Wallet.balance is incremented via a bulk F() update, mirroring
    apps/pv_ledger/services.py::record_purchase_pv's reasoning -- the
    increment happens entirely inside one DB statement, so it's race-free
    without select_for_update. This test proves it: five concurrent
    credits of GHS 10 each (from five distinct purchase events, hence
    five distinct references -- credit() doesn't retry internally, so
    each simulated caller wraps its own call in retry_on_lock_contention,
    exactly as a real caller outside an already-retried context must)
    must sum to exactly GHS 50, not less."""
    distributor = _make_distributor()

    def attempt(i):
        try:
            retry_on_lock_contention(
                lambda: credit(
                    distributor,
                    Decimal("10.00"),
                    transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
                    reference=f"concurrent-ref-{i}",
                )
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("50.00")
    assert WalletTransaction.objects.filter(wallet=wallet).count() == 5


@pytest.mark.django_db
def test_credit_rejects_a_negative_amount():
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        credit(
            distributor,
            Decimal("-10.00"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="ref-1",
        )


@pytest.mark.django_db
def test_credit_rejects_a_zero_amount():
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        credit(
            distributor,
            Decimal("0"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="ref-1",
        )


def test_credit_rejects_a_none_distributor():
    with pytest.raises(TypeError):
        credit(
            None,
            Decimal("10.00"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="ref-1",
        )


@pytest.mark.django_db
def test_credit_rejects_an_unknown_transaction_type():
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        credit(
            distributor,
            Decimal("10.00"),
            transaction_type="not_a_real_type",
            reference="ref-1",
        )


@pytest.mark.django_db
def test_the_same_reference_cannot_double_credit_the_same_wallet():
    """Defense-in-depth: the primary idempotency guard lives at the call
    site (e.g. consume_paid_starter_pack's starter_pack_confirmed_at
    check), but a DB-level constraint stops a future call-site bug from
    silently double-crediting for the same event."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="dup-ref",
    )

    with pytest.raises(IntegrityError):
        credit(
            distributor,
            Decimal("50.00"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="dup-ref",
        )
