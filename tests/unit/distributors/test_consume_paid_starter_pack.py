import threading
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

import pytest
from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.commissions.services import calculate_direct_referral_bonus
from apps.distributors.models import Distributor, PaymentIssue, StarterPackCheckout
from apps.distributors.payment_outcomes import PaymentOutcome
from apps.distributors.paystack import PaystackError
from apps.distributors.services import consume_paid_starter_pack
from apps.pv_ledger.models import MonthlyPersonalPv, PvLedger
from apps.pv_ledger.services import record_personal_pv as _real_record_personal_pv
from apps.pv_ledger.services import record_purchase_pv as _real_record_purchase_pv
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(sponsor=None):
    phone = f"+233241{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _select_pack_b(distributor):
    distributor.starter_pack_choice = "B"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_B_PV
    distributor.starter_pack_rank = config.STARTER_PACK_B_RANK
    distributor.starter_pack_payment_reference = "pack-ref-1"
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    return distributor


def _success_verify(amount, currency="GHS"):
    return {"status": "success", "amount": amount, "currency": currency}


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_b_purchase_sets_silver_rank_and_1000_pv(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000)  # GHS 2,000

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == "silver"
    assert distributor.starter_pack_pv == 1000
    assert distributor.starter_pack_confirmed_at is not None


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_a_purchase_sets_bronze_rank_and_500_pv(mock_verify):
    distributor = _make_distributor()
    distributor.starter_pack_choice = "A"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_A_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_A_PV
    distributor.starter_pack_rank = config.STARTER_PACK_A_RANK
    distributor.starter_pack_payment_reference = "pack-ref-a"
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    mock_verify.return_value = _success_verify(amount=150000)  # GHS 1,500

    consume_paid_starter_pack("pack-ref-a")

    distributor.refresh_from_db()
    assert distributor.rank == "bronze"
    assert distributor.starter_pack_pv == 500


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_already_confirmed_is_an_idempotent_no_op(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")
    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == "silver"
    assert mock_verify.call_count == 1  # second call short-circuits


@pytest.mark.django_db
def test_missing_distributor_reference_does_not_crash():
    consume_paid_starter_pack("no-such-reference")  # must not raise


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_non_success_status_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = {"status": "failed", "amount": 200000, "currency": "GHS"}

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""
    assert distributor.starter_pack_confirmed_at is None


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_wrong_currency_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000, currency="NGN")

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_mismatch_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=100000)  # wrong amount

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paystack_verify_error_does_not_crash(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.side_effect = PaystackError("timed out")

    consume_paid_starter_pack("pack-ref-1")  # must not raise

    distributor.refresh_from_db()
    assert distributor.rank == ""


# --- Task 10d: binary tree placement + PV ledger credit ---------------------


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_purchase_places_distributor_under_their_sponsor(mock_verify):
    sponsor = _make_distributor()
    distributor = _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    edge = BinaryTreeEdge.objects.get(descendant=distributor)
    assert edge.ancestor_id == sponsor.pk
    assert edge.depth == 1


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_b_purchase_credits_1000_pv_to_sponsors_weaker_leg(
    mock_verify,
):
    """Section 14 steps 4-7, reproduced exactly: Kofi joins under Ama, buys
    Pack B (GHS 2,000 / 1,000 PV), and lands on Ama's right leg because her
    left leg already carries more PV (weaker-leg auto-balance, since Task
    10a's registration form has no explicit leg-choice field yet)."""
    ama = _make_distributor()
    PvLedger.objects.create(distributor=ama, left_leg_pv=500, right_leg_pv=0)
    kofi = _select_pack_b(_make_distributor(sponsor=ama))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    edge = BinaryTreeEdge.objects.get(descendant=kofi)
    assert edge.leg == BinaryTreeEdge.Leg.RIGHT

    ama_ledger = PvLedger.objects.get(distributor=ama)
    assert ama_ledger.left_leg_pv == 500
    assert ama_ledger.right_leg_pv == 1000


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
@patch(
    "apps.distributors.services.record_personal_pv",
    wraps=_real_record_personal_pv,
)
@patch(
    "apps.distributors.services.record_purchase_pv",
    wraps=_real_record_purchase_pv,
)
def test_pv_credit_and_starter_pack_confirmed_at_share_one_pinned_now(
    mock_record_purchase_pv, mock_record_personal_pv, mock_verify
):
    """Date-drift bug (Task 19's doubt-driven-development review, found
    while designing the cooling-off refund's PV reversal): record_purchase_pv
    and record_personal_pv were each called with no explicit `today`,
    independently defaulting to their own timezone.now().date() call,
    while starter_pack_confirmed_at was set via a separate, independent
    timezone.now() call -- two unrelated reads of "now" that could
    resolve to different calendar dates at a UTC-midnight boundary.
    record_purchase_pv/record_personal_pv's own `today` parameter (Task
    18b) exists specifically so a later PV reversal can target the exact
    PvDailyBucket/MonthlyPersonalPv row the original credit landed in --
    Task 19's cooling-off reversal uses starter_pack_confirmed_at.date()
    as that anchor, so it must always match the date the credit actually
    used, not drift from it.

    Wraps (not replaces) both functions so the real implementation still
    runs -- this test's job is to check WHAT ARGUMENT they were called
    with, not to fake their behavior. (Globally mocking timezone.now
    itself was tried and rejected: django-simple-history's own internal
    HistoricalDistributor snapshot also calls it on every Distributor
    .save(), and feeding it a raw MagicMock crashes a real SQL INSERT --
    unrelated to what this test is actually about.)"""
    ama = _make_distributor()
    PvLedger.objects.create(distributor=ama, left_leg_pv=0, right_leg_pv=0)
    kofi = _select_pack_b(_make_distributor(sponsor=ama))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    kofi.refresh_from_db()
    confirmed_date = kofi.starter_pack_confirmed_at.date()
    assert mock_record_purchase_pv.call_args.kwargs.get("today") == confirmed_date
    assert mock_record_personal_pv.call_args.kwargs.get("today") == confirmed_date


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_pv_credit_propagates_up_multiple_ancestor_levels(mock_verify):
    grandparent = _make_distributor()
    parent = _make_distributor(sponsor=grandparent)
    from apps.binary_tree.services import BinaryTree

    BinaryTree.place_distributor(grandparent, parent, leg=BinaryTreeEdge.Leg.LEFT)
    distributor = _select_pack_b(_make_distributor(sponsor=parent))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    parent_edge = BinaryTreeEdge.objects.get(ancestor=parent, descendant=distributor)
    grandparent_edge = BinaryTreeEdge.objects.get(
        ancestor=grandparent, descendant=distributor
    )
    assert grandparent_edge.depth == 2

    parent_ledger = PvLedger.objects.get(distributor=parent)
    grandparent_ledger = PvLedger.objects.get(distributor=grandparent)
    parent_field = (
        "left_leg_pv" if parent_edge.leg == BinaryTreeEdge.Leg.LEFT else "right_leg_pv"
    )
    grandparent_field = (
        "left_leg_pv"
        if grandparent_edge.leg == BinaryTreeEdge.Leg.LEFT
        else "right_leg_pv"
    )
    assert getattr(parent_ledger, parent_field) == 1000
    assert getattr(grandparent_ledger, grandparent_field) == 1000


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirming_twice_does_not_double_place_or_double_credit_pv(mock_verify):
    sponsor = _make_distributor()
    distributor = _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")
    consume_paid_starter_pack("pack-ref-1")

    assert BinaryTreeEdge.objects.filter(descendant=distributor).count() == 1
    edge = BinaryTreeEdge.objects.get(descendant=distributor)
    field = "left_leg_pv" if edge.leg == BinaryTreeEdge.Leg.LEFT else "right_leg_pv"
    assert getattr(PvLedger.objects.get(distributor=sponsor), field) == 1000


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_root_distributor_with_no_sponsor_is_confirmed_without_placement(mock_verify):
    distributor = _select_pack_b(_make_distributor(sponsor=None))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")  # must not raise

    distributor.refresh_from_db()
    assert distributor.rank == "silver"
    assert not BinaryTreeEdge.objects.filter(descendant=distributor).exists()
    assert PvLedger.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.distributors.services.verify_transaction")
def test_concurrent_confirmations_under_the_same_sponsor_do_not_lose_pv(mock_verify):
    """Placement + PV credit run inside the *same* retry_on_lock_contention
    block as consume_paid_starter_pack's own idempotency check -- this means
    BinaryTree.place_distributor's own retry_on_lock_contention runs nested
    inside an already-open atomic() block (a savepoint, not a fresh
    transaction). This test exercises that nesting under real contention:
    two distributors sharing a sponsor confirm concurrently, both needing
    the sponsor's row. Like test_concurrent_placements_under_the_same_sponsor
    in tests/unit/binary_tree/test_placement.py, SQLite doesn't support real
    row locking (has_select_for_update is False), so this only proves the
    nesting works correctly under genuine lock contention once run against
    MySQL in CI -- but it still catches a regression in the surrounding
    retry/lock wiring even on SQLite, since each thread gets its own
    connection."""
    sponsor = _make_distributor()
    first = _select_pack_b(_make_distributor(sponsor=sponsor))
    first.starter_pack_payment_reference = "pack-ref-first"
    first.save(update_fields=["starter_pack_payment_reference"])
    second = _select_pack_b(_make_distributor(sponsor=sponsor))
    second.starter_pack_payment_reference = "pack-ref-second"
    second.save(update_fields=["starter_pack_payment_reference"])
    mock_verify.return_value = _success_verify(amount=200000)

    def attempt(reference):
        try:
            consume_paid_starter_pack(reference)
        finally:
            connection.close()  # each thread must not share the main
            # thread's connection/transaction state

    threads = [
        threading.Thread(target=attempt, args=("pack-ref-first",)),
        threading.Thread(target=attempt, args=("pack-ref-second",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert BinaryTreeEdge.objects.filter(ancestor=sponsor, depth=1).count() == 2

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.left_leg_pv + ledger.right_leg_pv == 2000


# --- Task 13a: monthly personal PV credited to the purchasing distributor --


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_purchase_credits_the_purchasers_own_personal_pv(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    row = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert row.pv == 1000


# --- Task 12b: Direct Referral Bonus credited to the sponsor ----------------


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_b_purchase_credits_sponsor_ghs_100(mock_verify):
    sponsor = _make_distributor()
    _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    wallet = Wallet.objects.get(distributor=sponsor)
    assert wallet.balance == Decimal("100.00")
    transaction = WalletTransaction.objects.get(wallet=wallet)
    assert (
        transaction.transaction_type
        == WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS
    )
    assert transaction.reference == "pack-ref-1"


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_b_purchase_logs_the_referral_bonus_credit(mock_verify, caplog):
    sponsor = _make_distributor()
    referred = _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    with caplog.at_level("INFO"):
        consume_paid_starter_pack("pack-ref-1")

    assert "100" in caplog.text
    assert str(sponsor.pk) in caplog.text
    assert str(referred.pk) in caplog.text
    assert "pack-ref-1" in caplog.text


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_root_distributor_with_no_sponsor_credits_nothing(mock_verify):
    _select_pack_b(_make_distributor(sponsor=None))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")  # must not raise

    assert not Wallet.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirming_twice_does_not_double_credit_the_referral_bonus(mock_verify):
    sponsor = _make_distributor()
    _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")
    consume_paid_starter_pack("pack-ref-1")

    wallet = Wallet.objects.get(distributor=sponsor)
    assert wallet.balance == Decimal("100.00")


# --- Task 12c: sponsor SMS notification on referral bonus -------------------


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_sponsor_is_notified_with_the_amount_and_referred_distributors_name(
    mock_verify,
):
    from apps.notifications.sms import fake_outbox

    sponsor = _make_distributor()
    referred = _select_pack_b(_make_distributor(sponsor=sponsor))
    referred.full_name = "Kofi Mensah"
    referred.save(update_fields=["full_name"])
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    assert fake_outbox, "no SMS was sent to the sponsor"
    message = fake_outbox[-1]
    assert message["phone_number"] == str(sponsor.phone_number)
    assert "100" in message["message"]
    assert "Kofi Mensah" in message["message"]


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_sponsor_notification_falls_back_to_phone_number_with_no_full_name(
    mock_verify,
):
    from apps.notifications.sms import fake_outbox

    sponsor = _make_distributor()
    referred = _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")

    assert str(referred.phone_number) in fake_outbox[-1]["message"]


@pytest.mark.django_db
@patch("apps.distributors.services.send_sms")
@patch("apps.distributors.services.verify_transaction")
def test_a_failed_sms_send_does_not_undo_the_wallet_credit(mock_verify, mock_send_sms):
    """The bonus already landed in the sponsor's wallet -- an SMS
    provider outage must not roll back real money that was correctly
    credited."""
    sponsor = _make_distributor()
    _select_pack_b(_make_distributor(sponsor=sponsor))
    mock_verify.return_value = _success_verify(amount=200000)
    mock_send_sms.side_effect = Exception("SMS provider is down")

    consume_paid_starter_pack("pack-ref-1")  # must not raise

    wallet = Wallet.objects.get(distributor=sponsor)
    assert wallet.balance == Decimal("100.00")


# --- Task 68c/68d: outcomes, and an issue recorded where it happens -----------
#
# Until now this returned None whatever happened, so the webhook answered 200
# even on a Paystack timeout -- burning the one push Paystack guarantees --
# and a paid-but-unapplied pack left nothing on the admin's Payments screen
# until the next day's reconciliation.


@pytest.fixture(autouse=True)
def _run_on_commit_now(monkeypatch):
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_confirmed_pack_reports_applied(mock_verify):
    distributor = _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.return_value = _success_verify(distributor.starter_pack_price_pesewas)

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.APPLIED


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_replay_reports_already_applied(mock_verify):
    distributor = _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.return_value = _success_verify(distributor.starter_pack_price_pesewas)
    consume_paid_starter_pack("pack-ref-1")

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.ALREADY_APPLIED


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_verify_failure_reports_it_so_paystack_is_asked_again(mock_verify):
    _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.side_effect = PaystackError("timed out")

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.VERIFY_FAILED
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_unpaid_reference_reports_not_paid(mock_verify):
    _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.return_value = {"status": "abandoned"}

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.NOT_PAID
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_wrong_amount_records_an_issue_immediately(mock_verify):
    """Not a log line the admin never reads, and not a day later."""
    _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.return_value = _success_verify(5000)

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.ISSUE_RECORDED

    issue = PaymentIssue.objects.get(reference="pack-ref-1")
    assert issue.kind == PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_wrong_currency_records_an_issue_immediately(mock_verify):
    distributor = _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    mock_verify.return_value = _success_verify(
        distributor.starter_pack_price_pesewas, currency="NGN"
    )

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.ISSUE_RECORDED
    assert PaymentIssue.objects.filter(reference="pack-ref-1").exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_paid_pack_with_no_distributor_records_an_issue(mock_verify):
    """An orphaned checkout from an earlier pack selection."""
    mock_verify.return_value = _success_verify(10000)

    assert consume_paid_starter_pack("pack-9-old") == PaymentOutcome.ISSUE_RECORDED

    assert (
        PaymentIssue.objects.get(reference="pack-9-old").kind
        == PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED
    )


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_unpaid_reference_with_no_distributor_records_nothing(mock_verify):
    mock_verify.return_value = {"status": "abandoned"}

    assert consume_paid_starter_pack("pack-9-old") == PaymentOutcome.NOT_PAID
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_payment_after_cooling_off_cancellation_records_an_issue(mock_verify):
    """The member is gone, but the money is real -- it needs refunding."""
    distributor = _select_pack_b(_make_distributor(sponsor=_make_distributor()))
    Distributor.objects.filter(pk=distributor.pk).update(
        cooling_off_cancelled_at=timezone.now()
    )
    mock_verify.return_value = _success_verify(distributor.starter_pack_price_pesewas)

    assert consume_paid_starter_pack("pack-ref-1") == PaymentOutcome.ISSUE_RECORDED
    assert PaymentIssue.objects.filter(reference="pack-ref-1").exists()


# --- Task 68b: an earlier checkout is never orphaned --------------------------


def _reselect_pack(
    distributor, reference, *, choice="A", price_pesewas=150000, pv=None, rank=None
):
    """What a back-button re-selection does: a new reference replaces the
    old one on the distributor."""
    distributor.starter_pack_choice = choice
    distributor.starter_pack_price_pesewas = price_pesewas
    distributor.starter_pack_pv = config.STARTER_PACK_A_PV if pv is None else pv
    distributor.starter_pack_rank = config.STARTER_PACK_A_RANK if rank is None else rank
    distributor.starter_pack_payment_reference = reference
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    StarterPackCheckout.objects.create(
        distributor=distributor,
        reference=reference,
        amount_pesewas=price_pesewas,
        choice=choice,
        pv=distributor.starter_pack_pv,
        rank=distributor.starter_pack_rank,
    )


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paying_on_an_earlier_checkout_still_applies_the_pack(mock_verify):
    """Pick a pack, open Paystack, go back, pick again, then pay on the first
    tab. Before Task 68b that money matched nothing at all."""
    distributor = _make_distributor(sponsor=_make_distributor())
    _reselect_pack(distributor, "pack-first", price_pesewas=150000)
    _reselect_pack(distributor, "pack-second", price_pesewas=150000)
    mock_verify.return_value = _success_verify(150000)

    assert consume_paid_starter_pack("pack-first") == PaymentOutcome.APPLIED

    distributor.refresh_from_db()
    assert distributor.starter_pack_confirmed_at is not None


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_the_amount_is_checked_against_that_checkouts_own_price(mock_verify):
    """An admin changing the pack price mid-checkout must not turn a correct
    payment into a rejected one."""
    distributor = _make_distributor(sponsor=_make_distributor())
    _reselect_pack(distributor, "pack-old-price", price_pesewas=150000)
    _reselect_pack(distributor, "pack-new-price", price_pesewas=200000)
    mock_verify.return_value = _success_verify(150000)  # paid the old price

    assert consume_paid_starter_pack("pack-old-price") == PaymentOutcome.APPLIED


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paying_twice_on_two_checkouts_applies_once_and_reports_the_second(
    mock_verify,
):
    distributor = _make_distributor(sponsor=_make_distributor())
    _reselect_pack(distributor, "pack-first", price_pesewas=150000)
    _reselect_pack(distributor, "pack-second", price_pesewas=150000)
    mock_verify.return_value = _success_verify(150000)
    consume_paid_starter_pack("pack-first")

    outcome = consume_paid_starter_pack("pack-second")

    assert outcome == PaymentOutcome.ISSUE_RECORDED
    assert PaymentIssue.objects.filter(reference="pack-second").exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paying_an_older_checkout_gives_that_pack_not_the_newer_one(mock_verify):
    """Agent code review of 68b: checking the amount against the paid
    checkout while applying the CURRENT selection would hand out Pack B's PV,
    rank and referral bonus for Pack A's money -- self-service and
    repeatable."""
    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    _reselect_pack(
        distributor,
        "pack-cheap",
        choice="A",
        price_pesewas=50000,
        pv=500,
        rank="bronze",
    )
    _reselect_pack(
        distributor,
        "pack-dear",
        choice="B",
        price_pesewas=150000,
        pv=1000,
        rank="silver",
    )
    mock_verify.return_value = _success_verify(50000)  # paid the cheap one

    assert consume_paid_starter_pack("pack-cheap") == PaymentOutcome.APPLIED

    distributor.refresh_from_db()
    assert distributor.rank == "bronze"
    assert MonthlyPersonalPv.objects.get(distributor=distributor).pv == 500
    # The sponsor's bonus follows the pack that was paid for, too.
    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == calculate_direct_referral_bonus(500)


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_the_paid_pack_is_written_back_so_a_refund_cannot_pay_out_more(mock_verify):
    """Third security review: applying the paid checkout but leaving the
    distributor's stored price/PV on the newer selection meant the
    cooling-off refund paid out the dearer pack's price for the cheaper
    pack's money, and stripped PV from uplines that was never credited."""
    distributor = _make_distributor(sponsor=_make_distributor())
    _reselect_pack(
        distributor,
        "pack-cheap",
        choice="A",
        price_pesewas=50000,
        pv=500,
        rank="bronze",
    )
    _reselect_pack(
        distributor,
        "pack-dear",
        choice="B",
        price_pesewas=200000,
        pv=1000,
        rank="silver",
    )
    mock_verify.return_value = _success_verify(50000)

    consume_paid_starter_pack("pack-cheap")

    distributor.refresh_from_db()
    assert distributor.starter_pack_price_pesewas == 50000
    assert distributor.starter_pack_pv == 500
    assert distributor.starter_pack_choice == "A"
    assert distributor.starter_pack_rank == "bronze"
