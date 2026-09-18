import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.pv_ledger.services import reverse_ancestor_pv
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit as credit_wallet
from apps.wallet.services import debit as debit_wallet
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import Distributor, StarterPackCheckout

logger = logging.getLogger(__name__)


class NoRefundableStarterPackPurchase(Exception):
    """Raised when a distributor has no confirmed starter-pack purchase
    to cancel -- e.g. they registered but never completed payment."""


class CoolingOffPeriodExpired(Exception):
    """Raised when the request arrives more than COOLING_OFF_PERIOD_DAYS
    after starter_pack_confirmed_at (ADR-0007 Decision 1)."""


def calculate_cooling_off_refund(starter_pack_price_pesewas: int) -> Decimal:
    """ADR-0007 Decision 5, verified against the source doc's own worked
    example: Pack B GHS 2,000, rate 10% -> GHS 1,800. Extracted as its own
    function (Task 19c) so the distributor-facing preview
    (apps/distributors/views.py::cancel_membership's GET) and the actual
    refund credited by cancel_membership_and_refund below always agree --
    a single source of truth rather than two independently-maintained
    copies of the same formula."""
    return (
        Decimal(starter_pack_price_pesewas)
        / Decimal("100")
        * (Decimal("1") - config.COOLING_OFF_REFUND_DEDUCTION_RATE / Decimal("100"))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cancel_membership_and_refund(distributor_id) -> Decimal | None:
    """Task 19b (ADR-0007): a distributor's self-service 7-day cooling-off
    cancellation. Reverses the PV and sponsor's direct referral bonus
    credited at starter-pack confirmation, refunds
    `starter_pack_price_pesewas * (1 - COOLING_OFF_REFUND_DEDUCTION_RATE / 100)`
    to the distributor's own wallet, clears their rank/starter-pack
    fields, and deactivates their account. BinaryTreeEdge placement is
    deliberately left untouched (ADR-0007 Decision 2 -- no removal
    mechanism exists anywhere in this codebase).

    Idempotent: a second call for an already-cancelled distributor is a
    safe no-op, guarded by `cooling_off_cancelled_at` -- deliberately a
    field distinct from `user.is_active`, since that field is also used
    by an unrelated admin suspend/reactivate toggle
    (apps/admin_portal/views.py) that has no knowledge of cooling-off
    cancellation. `snapshot_starter_pack_choice` also checks
    `cooling_off_cancelled_at` directly, so even an admin reactivation
    can't reopen a path to re-crediting PV/bonus for this same
    membership (see MembershipCancelled's docstring).

    Locking (doubt-driven-development review, pre-implementation): the
    cancelling distributor's own row and every one of their
    BinaryTreeEdge ancestor rows are collected into ONE combined,
    ascending-pk-sorted lock set up front -- mirroring
    BinaryTree.place_distributor's and reverse_ancestor_pv's own "single
    fixed order across the whole lock set" convention exactly, rather
    than locking the distributor's own row as a separate, out-of-order
    step (which could invert lock order against a concurrent
    cancellation where one distributor is an ancestor of the other).
    reverse_ancestor_pv's own internal ancestor-locking is therefore a
    harmless re-acquisition of locks this function already holds, not a
    second, differently-ordered lock attempt. The sponsor's Wallet row
    (a different table) is locked last, via the same NOWAIT convention
    (`select_for_update_nowait_if_supported`, not a plain blocking
    `select_for_update()` -- a blocking wait risks tens of seconds held
    against every other lock this function already has, per
    bancostore/concurrency.py's own documented reasoning), specifically
    so the "debit whatever is available" partial-debit read-then-decide
    sequence below is race-free against a concurrent debit of the same
    sponsor's wallet (e.g. a withdrawal approval).

    Returns the refund amount (Decimal, GHS) on a real cancellation, or
    None for an idempotent no-op (already cancelled)."""

    def _attempt():
        with transaction.atomic():
            ancestor_ids = set(
                BinaryTreeEdge.objects.filter(descendant_id=distributor_id).values_list(
                    "ancestor_id", flat=True
                )
            )
            lock_ids = sorted(ancestor_ids | {distributor_id})
            locked_by_pk = {}
            for pk in lock_ids:
                locked_by_pk[pk] = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=pk)
                ).get()
            distributor = locked_by_pk[distributor_id]

            if distributor.cooling_off_cancelled_at is not None:
                logger.info(
                    "cancel_membership_and_refund: distributor_id=%s already "
                    "cancelled at %s -- idempotent no-op.",
                    distributor_id,
                    distributor.cooling_off_cancelled_at,
                )
                return None

            if distributor.starter_pack_confirmed_at is None:
                raise NoRefundableStarterPackPurchase(
                    f"distributor_id={distributor_id} has no confirmed "
                    "starter-pack purchase to cancel"
                )

            now = timezone.now()
            deadline = distributor.starter_pack_confirmed_at + timedelta(
                days=config.COOLING_OFF_PERIOD_DAYS
            )
            if now > deadline:
                raise CoolingOffPeriodExpired(
                    f"distributor_id={distributor_id} cooling-off window "
                    f"expired at {deadline}"
                )

            refund_amount = calculate_cooling_off_refund(
                distributor.starter_pack_price_pesewas
            )

            purchase_date = distributor.starter_pack_confirmed_at.date()
            # Reverses ancestor-leg PvLedger/PvDailyBucket AND this
            # distributor's own MonthlyPersonalPv in one call -- the same
            # two events (record_purchase_pv + record_personal_pv)
            # consume_paid_starter_pack originally credited separately.
            reverse_ancestor_pv(distributor, distributor.starter_pack_pv, purchase_date)

            reference = f"cooling-off-{distributor.pk}"

            if distributor.sponsor_id:
                _reverse_direct_referral_bonus(distributor, reference)

            credit_wallet(
                distributor,
                refund_amount,
                transaction_type=WalletTransaction.TransactionType.COOLING_OFF_REFUND,
                reference=reference,
            )

            distributor.cooling_off_cancelled_at = now
            distributor.rank = ""
            distributor.starter_pack_choice = ""
            distributor.starter_pack_price_pesewas = None
            distributor.starter_pack_pv = None
            distributor.starter_pack_rank = ""
            distributor.starter_pack_confirmed_at = None
            distributor.save(
                update_fields=[
                    "cooling_off_cancelled_at",
                    "rank",
                    "starter_pack_choice",
                    "starter_pack_price_pesewas",
                    "starter_pack_pv",
                    "starter_pack_rank",
                    "starter_pack_confirmed_at",
                ]
            )

            distributor.user.is_active = False
            distributor.user.save(update_fields=["is_active"])

            logger.info(
                "cancel_membership_and_refund: distributor_id=%s cancelled, "
                "refund=%s credited, account deactivated.",
                distributor.pk,
                refund_amount,
            )
            return refund_amount

    return retry_on_lock_contention(_attempt)


def _credited_pack_reference(distributor) -> str:
    """Which checkout's payment actually credited the sponsor's bonus. Since
    Task 68b a distributor can have several starter-pack checkouts and pay an
    older one, so the current starter_pack_payment_reference is only right
    when nothing was ever re-selected."""
    consumed = (
        StarterPackCheckout.objects.filter(
            distributor_id=distributor.pk, consumed_at__isnull=False
        )
        .order_by("-consumed_at", "-pk")
        .first()
    )
    if consumed is not None:
        return consumed.reference
    return distributor.starter_pack_payment_reference


def _reverse_direct_referral_bonus(
    distributor, reference: str, credited_reference: str | None = None
) -> bool:
    """ADR-0007 Decisions 3/4: reverses the ONE-TIME direct referral bonus
    `distributor.sponsor` was credited at starter-pack confirmation --
    nothing else. Called from inside cancel_membership_and_refund's own
    locked block; does no locking of its own beyond the sponsor's Wallet
    row.

    The amount reversed is looked up from the actual WalletTransaction
    the original credit created (doubt-driven-development finding,
    pre-implementation review), NOT recomputed via
    calculate_direct_referral_bonus(distributor.starter_pack_pv) -- that
    function reads the LIVE DIRECT_REFERRAL_BONUS_RATE, which may have
    changed since signup. Every other money figure in this codebase is
    snapshotted at the original event for exactly this reason
    (starter_pack_price_pesewas, PendingRegistration.fee_amount_pesewas,
    every Order line item) -- recomputing here would silently claw back
    the wrong amount after any admin rate change.

    If the sponsor's wallet balance is less than the bonus (e.g. they
    already withdrew it), debits whatever is currently available --
    possibly zero -- and logs the shortfall. A distributor's statutory
    cancellation right must not be blocked by an unrelated wallet state
    (ADR-0007 Decision 4, mirroring ADR-0006's identical
    already-paid-out-is-an-accepted-limitation precedent)."""
    # Task 68 (adversarial security review): the credit was written against
    # the checkout that was ACTUALLY paid, which since 68b is not always the
    # distributor's current starter_pack_payment_reference -- a back-button
    # re-selection leaves them different. Looking the credit up by the
    # current reference silently found nothing, returned quietly, and let
    # the caller report a clawback that never happened.
    credited_reference = credited_reference or _credited_pack_reference(distributor)
    original_bonus_txn = WalletTransaction.objects.filter(
        wallet__distributor_id=distributor.sponsor_id,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference=credited_reference,
    ).first()
    if original_bonus_txn is None:
        logger.error(
            "cancel_membership_and_refund: distributor_id=%s sponsor_id=%s "
            "has sponsor_id set but no matching DIRECT_REFERRAL_BONUS "
            "WalletTransaction found for reference=%s -- skipping bonus "
            "reversal, distributor's own refund proceeds regardless. Needs "
            "manual investigation.",
            distributor.pk,
            distributor.sponsor_id,
            credited_reference,
        )
        return False

    bonus = original_bonus_txn.amount

    try:
        sponsor_wallet = select_for_update_nowait_if_supported(
            Wallet.objects.filter(distributor_id=distributor.sponsor_id)
        ).get()
    except Wallet.DoesNotExist:
        logger.error(
            "cancel_membership_and_refund: distributor_id=%s sponsor_id=%s "
            "has a DIRECT_REFERRAL_BONUS WalletTransaction but no Wallet row "
            "-- skipping bonus reversal, distributor's own refund proceeds "
            "regardless. Needs manual investigation.",
            distributor.pk,
            distributor.sponsor_id,
        )
        return False

    debit_amount = min(sponsor_wallet.balance, bonus)
    if debit_amount > 0:
        debit_wallet(
            distributor.sponsor,
            debit_amount,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL,
            reference=reference,
        )
    if debit_amount < bonus:
        logger.warning(
            "cancel_membership_and_refund: distributor_id=%s sponsor_id=%s "
            "direct referral bonus reversal short by %s (available=%s, "
            "owed=%s) -- accepted gap per ADR-0007, logged for manual admin "
            "follow-up.",
            distributor.pk,
            distributor.sponsor_id,
            bonus - debit_amount,
            sponsor_wallet.balance,
            bonus,
        )
    return debit_amount > 0
