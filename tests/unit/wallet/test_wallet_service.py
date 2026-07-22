import threading
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.db.models import F, Sum

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import InsufficientBalanceError, credit, debit
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
def test_credit_logs_the_distributor_amount_type_and_reference(caplog):
    """The on-call question this answers: "was this wallet actually
    credited, for how much, and from what?" -- without a log line, that's
    a database query, not a log search."""
    distributor = _make_distributor()

    with caplog.at_level("INFO"):
        credit(
            distributor,
            Decimal("50.00"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference="starter-pack-ref-1",
        )

    assert str(distributor.pk) in caplog.text
    assert "50.00" in caplog.text
    assert "direct_referral_bonus" in caplog.text
    assert "starter-pack-ref-1" in caplog.text


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


# ---------------------------------------------------------------------------
# debit() -- Task 15, symmetric to credit() above. Stores a NEGATIVE amount
# so Wallet.balance always equals SUM(WalletTransaction.amount) for that
# wallet (the literal "balance is always the sum of ledger entries"
# acceptance criterion), and is race-safe via one atomic conditional UPDATE
# (balance__gte=amount), the same F()-relative-update reasoning credit()
# already relies on -- not a separate locking read.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_debit_decreases_balance_and_records_a_negative_ledger_entry():
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="fund-1",
    )

    debit(
        distributor,
        Decimal("30.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("70.00")
    debit_txn = WalletTransaction.objects.get(wallet=wallet, reference="withdrawal-1")
    assert debit_txn.amount == Decimal("-30.00")
    assert (
        debit_txn.transaction_type == WalletTransaction.TransactionType.WITHDRAWAL_DEBIT
    )


@pytest.mark.django_db
def test_a_second_debit_subtracts_further():
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="fund-1",
    )
    debit(
        distributor,
        Decimal("20.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )

    debit(
        distributor,
        Decimal("15.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-2",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("65.00")
    assert WalletTransaction.objects.filter(wallet=wallet).count() == 3


@pytest.mark.django_db
def test_debit_never_uses_a_float_for_the_amount():
    distributor = _make_distributor()

    with pytest.raises(TypeError):
        debit(
            distributor,
            30.0,
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="ref-1",
        )


@pytest.mark.django_db
def test_debit_rejects_a_negative_amount():
    """The public amount argument must still be positive, matching
    credit()'s own convention -- debit() negates it internally, the
    caller never passes a sign."""
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        debit(
            distributor,
            Decimal("-10.00"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="ref-1",
        )


@pytest.mark.django_db
def test_debit_rejects_a_zero_amount():
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        debit(
            distributor,
            Decimal("0"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="ref-1",
        )


def test_debit_rejects_a_none_distributor():
    with pytest.raises(TypeError):
        debit(
            None,
            Decimal("10.00"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="ref-1",
        )


@pytest.mark.django_db
def test_debit_rejects_an_unknown_transaction_type():
    distributor = _make_distributor()

    with pytest.raises(ValueError):
        debit(
            distributor,
            Decimal("10.00"),
            transaction_type="not_a_real_type",
            reference="ref-1",
        )


@pytest.mark.django_db
def test_debit_more_than_the_balance_is_rejected_and_nothing_is_written():
    """The whole point of the conditional atomic UPDATE: an over-debit
    must fail cleanly (a friendly InsufficientBalanceError, not a raw
    IntegrityError), and must not create a ledger row or touch the
    balance at all -- the atomic block rolls back both together."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="fund-1",
    )

    with pytest.raises(InsufficientBalanceError):
        debit(
            distributor,
            Decimal("50.01"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="withdrawal-1",
        )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("50.00")
    assert not WalletTransaction.objects.filter(reference="withdrawal-1").exists()


@pytest.mark.django_db
def test_debiting_the_exact_balance_to_zero_succeeds():
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="fund-1",
    )

    debit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("0.00")


@pytest.mark.django_db
def test_debit_against_a_wallet_that_never_had_a_credit_is_rejected():
    """No wallet exists yet -- get_or_create lazily makes one at balance
    0, and the conditional update must still correctly reject (0 >= any
    positive amount is false), not crash on a missing row."""
    distributor = _make_distributor()

    with pytest.raises(InsufficientBalanceError):
        debit(
            distributor,
            Decimal("10.00"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="withdrawal-1",
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_debits_never_overdraw_the_balance():
    """Mirrors test_concurrent_credits_never_lose_an_update's proof, for
    the new failure mode debit() introduces: five concurrent GHS 10
    debits against a GHS 30 balance must let exactly three succeed and
    two fail with InsufficientBalanceError -- never let more drain out
    than was actually available, and never leave the balance negative."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("30.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="fund-1",
    )
    errors = []
    lock = threading.Lock()

    def attempt(i):
        try:
            retry_on_lock_contention(
                lambda: debit(
                    distributor,
                    Decimal("10.00"),
                    transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
                    reference=f"concurrent-debit-{i}",
                )
            )
        except InsufficientBalanceError as exc:
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("0.00")
    assert len(errors) == 2
    assert (
        WalletTransaction.objects.filter(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        ).count()
        == 3
    )


# ---------------------------------------------------------------------------
# Balance invariant + the CheckConstraint backstop
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_balance_after_a_mixed_credit_debit_sequence_matches_the_ledger_sum():
    """The literal Task 15 acceptance criterion."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("200.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    credit(
        distributor,
        Decimal("45.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="ref-2",
    )
    debit(
        distributor,
        Decimal("60.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-3",
    )
    credit(
        distributor,
        Decimal("1.88"),
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
        reference="ref-4",
    )
    debit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-5",
    )

    wallet = Wallet.objects.get(distributor=distributor)
    ledger_sum = WalletTransaction.objects.filter(wallet=wallet).aggregate(
        total=Sum("amount")
    )["total"]
    assert wallet.balance == ledger_sum
    assert wallet.balance == Decimal("86.88")


@pytest.mark.django_db
def test_wallet_balance_check_constraint_rejects_a_negative_balance_directly():
    """Defense-in-depth, independent of debit()'s own conditional-update
    guard: mirrors PvDailyBucket's own CheckConstraint(pv__gte=0) reasoning
    exactly -- a bulk F()-relative UPDATE bypasses Django's model-level
    field validation entirely, so a future code path that touched
    Wallet.balance without debit()'s own balance__gte=amount discipline
    must still be caught at the DB level, not silently allowed."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("10.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = Wallet.objects.get(distributor=distributor)

    with pytest.raises(IntegrityError):
        Wallet.objects.filter(pk=wallet.pk).update(
            balance=F("balance") - Decimal("999")
        )


@pytest.mark.django_db
def test_the_same_reference_cannot_double_debit_the_same_wallet():
    """debit()'s own twin of test_the_same_reference_cannot_double_credit_
    the_same_wallet above -- same shared UniqueConstraint(wallet, reference,
    transaction_type), same defense-in-depth reasoning: the primary
    idempotency guard lives at whichever call site eventually calls
    debit() (Task 16/19), this is the backstop against a call-site bug
    retrying and double-debiting for the same event."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    debit(
        distributor,
        Decimal("20.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="dup-debit-ref",
    )

    with pytest.raises(IntegrityError):
        debit(
            distributor,
            Decimal("20.00"),
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
            reference="dup-debit-ref",
        )

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("80.00")
