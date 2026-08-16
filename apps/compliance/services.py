import logging
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from constance import config

from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.notifications.email import get_sender_email
from apps.notifications.models import NotificationTemplate
from apps.orders.models import Order
from apps.platform_settings.config import humanize_identifier_name
from apps.platform_settings.models import PlatformSettingChange
from apps.withdrawal.models import WithdrawalRequest
from bancostore.concurrency import select_for_update_nowait_if_supported

from .models import ComplianceAlertState, EscrowLedger, EscrowTransaction

logger = logging.getLogger(__name__)

# Order.Status values that never actually collected payment -- excluded
# from the retail/distributor ratio's denominator, matching this
# codebase's own established exclusion set (independently defined in
# apps.admin_portal.views.dashboard and apps.reporting.services rather
# than a shared import -- this project's own accepted precedent for this
# narrowly-scoped 3-tuple).
_UNPAID_STATUSES = (Order.Status.PENDING, Order.Status.CANCELLED, Order.Status.REFUNDED)


def credit_escrow(order) -> None:
    """Task 47a. Credits the platform-wide EscrowLedger (pk=1) by
    ESCROW_RESERVE_RATE% of `order`'s NET product revenue (subtotal
    minus discount_amount -- delivery_fee never enters this calculation;
    a discount reduces the money the platform actually collected for
    products, so escrow shouldn't be held against money never received).
    Order.subtotal - Order.discount_amount can never go negative --
    Order's own order_amounts_sane CheckConstraint already enforces
    discount_amount <= subtotal.

    Called unconditionally for EVERY confirmed order (guest, customer,
    AND distributor purchases alike) -- a doubt-driven-development
    finding against an earlier draft that would have placed this call
    inside confirm_order_payment's distributor-only PV-credit block,
    silently escrowing only distributor purchases. "Product revenue" has
    no such restriction.

    A zero rate (ESCROW_RESERVE_RATE == 0, a legitimate admin-configured
    "turn this off" state, exactly like WEEKLY_BINARY_BONUS_CAP=0
    elsewhere in this codebase) is an explicit no-op: no EscrowLedger
    update, no EscrowTransaction row. Without this, a 0% rate would
    compute amount=Decimal("0.00") and violate
    EscrowTransaction.escrow_transaction_sign_matches_type's own
    CheckConstraint for a CREDIT row -- inside confirm_order_payment's
    atomic block, which paystack_webhook and order_payment_callback both
    depend on never raising. A doubt-driven-development review rated
    this Critical.

    Does NOT retry on lock contention itself -- same contract as
    apps.wallet.services.credit/debit: always called from within an
    already-retried caller (confirm_order_payment's own
    retry_on_lock_contention(_attempt)). NOWAIT-locks the EscrowLedger
    row before updating it (unlike Wallet.credit's plain conditional
    UPDATE) -- a doubt-driven-development finding: this is a SINGLE
    platform-wide row every confirmed order contends for, unlike
    Wallet's naturally-sharded per-distributor rows, so a plain UPDATE
    risks blocking for up to MySQL's full lock-wait-timeout instead of
    failing fast into the existing retry budget."""
    rate = config.ESCROW_RESERVE_RATE
    net_product_revenue = order.subtotal - order.discount_amount
    amount = (net_product_revenue * rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if amount == 0:
        return

    with transaction.atomic():
        # get_or_create is the primary safety net only in the sense that
        # it makes this call robust regardless of DB state -- in every
        # real environment the migration-seeded row (0002_seed_escrow_
        # ledger.py) already exists by the time any traffic arrives,
        # mirroring apps.wallet.services.credit()'s own identical
        # lazy-creation shape for a distributor's Wallet.
        EscrowLedger.objects.get_or_create(pk=1)
        ledger = select_for_update_nowait_if_supported(
            EscrowLedger.objects.filter(pk=1)
        ).get()
        updated = EscrowLedger.objects.filter(pk=ledger.pk).update(
            balance=F("balance") + amount
        )
        if not updated:
            raise EscrowLedger.DoesNotExist(
                f"credit_escrow(): EscrowLedger pk={ledger.pk} disappeared "
                f"mid-credit"
            )
        EscrowTransaction.objects.create(
            ledger=ledger,
            order_id=order.pk,
            transaction_type=EscrowTransaction.TransactionType.CREDIT,
            amount=amount,
            rate_applied=rate,
        )
        # The on-call question this answers: "was this order's escrow
        # actually credited, for how much, at what rate?" -- without
        # this, that's a database query, not a log search.
        logger.info(
            "credit_escrow: order_id=%s amount=%s rate=%s", order.pk, amount, rate
        )


def reverse_escrow(order_id) -> None:
    """Task 47a. Reverses a previously-credited escrow amount when an
    order is cancelled/refunded -- a "rolling reserve" model (matching
    how real payment processors like Stripe/PayPal/Paystack handle
    merchant reserve holdbacks): the reserve was tied to a specific
    order's revenue, so refunding that order releases its share back.
    A user-confirmed design choice, deliberately NOT the accumulate-only
    alternative that was also considered (which would have mirrored
    Binary/Matching Bonus's own "never clawed back" precedent) -- those
    are third-party payouts, not a reserve held against a specific
    order's own revenue, so that precedent doesn't transfer here.

    Reverses the EXACT amount originally credited (looked up from the
    order's own CREDIT EscrowTransaction row via .get(), which fails
    loud with MultipleObjectsReturned rather than silently picking one
    if that one-credit-per-order invariant is ever violated), never
    recomputed from the current live ESCROW_RESERVE_RATE -- matching
    this codebase's established snapshot-at-event-time convention
    (Task 19's sponsor-bonus reversal uses the same reasoning).

    A no-op if no CREDIT row exists for this order (a legacy order from
    before this feature existed -- unreachable through normal use since
    cancel_or_refund_order only operates on already-CONFIRMED+ orders,
    which always credit escrow, per credit_escrow's own unconditional
    placement) or if a REVERSAL row already exists (idempotent no-op;
    defense-in-depth on top of the structural
    unique_escrow_transaction_order_type constraint and on top of
    cancel_or_refund_order's own Order-row lock, which is what actually
    makes a concurrent double-call on the same order_id unreachable in
    practice).

    MUST be called from within a transaction that already holds an
    exclusive lock scoped to this order_id for the duration of the call
    -- currently only apps.orders.services.cancel_or_refund_order
    provides this (via its own select_for_update_nowait_if_supported
    lock on the Order row). Calling this from an unlocked context
    reintroduces a real TOCTOU race on the already-reversed check below.
    Does NOT retry on lock contention itself -- same contract as
    credit_escrow above."""
    try:
        credit_txn = EscrowTransaction.objects.get(
            order_id=order_id,
            transaction_type=EscrowTransaction.TransactionType.CREDIT,
        )
    except EscrowTransaction.DoesNotExist:
        return

    already_reversed = EscrowTransaction.objects.filter(
        order_id=order_id,
        transaction_type=EscrowTransaction.TransactionType.REVERSAL,
    ).exists()
    if already_reversed:
        return

    with transaction.atomic():
        ledger = select_for_update_nowait_if_supported(
            EscrowLedger.objects.filter(pk=credit_txn.ledger_id)
        ).get()
        updated = EscrowLedger.objects.filter(pk=ledger.pk).update(
            balance=F("balance") - credit_txn.amount
        )
        if not updated:
            raise EscrowLedger.DoesNotExist(
                f"reverse_escrow(): EscrowLedger pk={ledger.pk} disappeared "
                f"mid-reversal"
            )
        EscrowTransaction.objects.create(
            ledger=ledger,
            order_id=order_id,
            transaction_type=EscrowTransaction.TransactionType.REVERSAL,
            amount=-credit_txn.amount,
            rate_applied=credit_txn.rate_applied,
        )
        logger.info(
            "reverse_escrow: order_id=%s amount=%s", order_id, -credit_txn.amount
        )


# ---------------------------------------------------------------------------
# Task 47b: retail/distributor ratio + compliance alert
# ---------------------------------------------------------------------------


def get_retail_distributor_ratio():
    """Task 47b. The percentage of currently-paid orders (excludes
    _UNPAID_STATUSES, matching this codebase's own revenue-figure
    convention) that are genuine retail sales -- Order.pv_earned == 0,
    the existing real distributor-purchase signal already established in
    apps.orders.services (a distributor order earns PV, a retail order
    never does). "Currently paid" reads live off Order.status, so a
    since-cancelled/refunded order correctly stops counting in either
    bucket -- and a cancelled order's pv_earned is reset to 0 by
    apps.orders.services.cancel_or_refund_order regardless, so it could
    never wrongly count as a distributor sale even if it weren't excluded
    by status alone.

    Returns None if there are no paid orders yet (the ratio is undefined,
    not zero) -- callers must handle this, never divide by zero."""
    paid_orders = Order.objects.exclude(status__in=_UNPAID_STATUSES)
    counts = paid_orders.aggregate(
        total=Count("pk"), retail=Count("pk", filter=Q(pv_earned=0))
    )
    if counts["total"] == 0:
        return None
    return (Decimal(counts["retail"]) / Decimal(counts["total"]) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def check_retail_ratio_and_alert() -> None:
    """Task 47b. Called after every order confirmation and cancellation/
    refund (the only two events that can move the ratio) -- always
    OUTSIDE the caller's own transaction.atomic() lock, since this sends
    real email (external I/O), matching this codebase's established
    "never hold a lock across external I/O for a notification" standard
    (Task 16g).

    Fires an email to COMPLIANCE_ALERT_EMAIL only on the TRANSITION from
    above/at-threshold to below RETAIL_PV_MINIMUM_PERCENT -- not on every
    call while still below, which would flood the configured inbox once
    persistently below threshold. See ComplianceAlertState's own
    docstring for the full reasoning. A blank COMPLIANCE_ALERT_EMAIL (the
    seeded default -- no admin has configured one yet) or an email
    failure is logged, never raised -- this must not break order
    confirmation/cancellation, which already committed by the time this
    runs.

    **Accepted limitation, not silently missed:** the read-then-write of
    ComplianceAlertState here is deliberately NOT lock-protected, since
    that would mean holding a lock across the send_mail() call itself --
    exactly what the Task 16g standard this docstring cites exists to
    prevent. Under genuinely concurrent order confirmations landing in
    the same narrow window, this can theoretically send one duplicate
    alert email rather than exactly one. Unlike a double-credited wallet
    or escrow balance, the consequence is a harmless duplicate
    notification, not a financial/data-integrity error -- judged
    disproportionate to add a redesign for, matching this codebase's own
    established pattern of documenting an accepted, narrow-blast-radius
    risk rather than engineering it away (e.g. PvDailyBucket's fungible-
    pool limitation, Task 13/14's deferred circuit breaker)."""
    ratio = get_retail_distributor_ratio()
    if ratio is None:
        return

    threshold = config.RETAIL_PV_MINIMUM_PERCENT
    currently_below = ratio < threshold
    state, _ = ComplianceAlertState.objects.get_or_create(pk=1)

    if currently_below and not state.is_below_threshold:
        recipient = config.COMPLIANCE_ALERT_EMAIL.strip()
        if recipient:
            try:
                send_mail(
                    subject="Bancostore compliance alert: retail ratio below threshold",
                    message=(
                        f"The retail/distributor sales ratio has dropped to "
                        f"{ratio}%, below the configured minimum of "
                        f"{threshold}%. Please review distributor purchase "
                        f"activity."
                    ),
                    from_email=get_sender_email(),
                    recipient_list=[recipient],
                )
                # Only set on a genuine successful send -- the state
                # transition below still records "we're now below
                # threshold" regardless of delivery outcome, so a blank
                # recipient or an SMTP failure doesn't cause this same
                # order to be re-evaluated as a fresh transition (and
                # therefore re-attempted) on every subsequent order while
                # still below threshold -- matching this codebase's
                # established "best effort, no retry storm" convention
                # for notification delivery (_send_confirmation_notifications).
                state.last_alert_sent_at = timezone.now()
            except Exception:
                logger.exception(
                    "check_retail_ratio_and_alert: failed to send alert email " "to %s",
                    recipient,
                )
        else:
            logger.warning(
                "check_retail_ratio_and_alert: ratio %s%% is below the %s%% "
                "threshold but COMPLIANCE_ALERT_EMAIL is not configured -- "
                "no alert sent.",
                ratio,
                threshold,
            )
        state.is_below_threshold = True
        state.save(update_fields=["is_below_threshold", "last_alert_sent_at"])
    elif not currently_below and state.is_below_threshold:
        state.is_below_threshold = False
        state.save(update_fields=["is_below_threshold"])


# ---------------------------------------------------------------------------
# Task 47e: unified audit log
# ---------------------------------------------------------------------------

# Every HistoricalRecords()-tracked model in this codebase.
# apps.compliance.tasks.cleanup_expired_audit_records imports this exact
# dict rather than a second hardcoded list (a CodeRabbit-caught drift
# risk on PR #78, fixed before it could recur) -- adding a model here
# wires it into both the unified audit log AND the retention cleanup
# job in one edit. NotificationTemplate added by Task 48a: it governs
# real customer-facing financial/KYC wording, the same class of
# admin-editable content this audit trail already covers.
_HISTORY_TRACKED_MODELS = {
    "Category": Category,
    "Product": Product,
    "Distributor": Distributor,
    "Order": Order,
    "WithdrawalRequest": WithdrawalRequest,
    "NotificationTemplate": NotificationTemplate,
}

# Display-only humanization of the internal keys above, applied where a
# real admin reads this screen -- deliberately NOT applied to
# _HISTORY_TRACKED_MODELS's own keys, which apps.compliance.tasks
# .cleanup_expired_audit_records also uses as its returned deleted_counts
# dict's keys (asserted verbatim by its own tests). Category/Product/
# Distributor/Order already read fine as one word; only the two
# multi-word model names need a space inserted.
_MODEL_DISPLAY_LABELS = {
    "WithdrawalRequest": "Withdrawal Request",
    "NotificationTemplate": "Notification Template",
}


def _day_start(target_date):
    """Timezone-aware midnight for a calendar date -- half-open [start,
    end) bounds instead of a `__date` lookup, matching the exact
    apps.reporting.services precedent (Task 46): `__date` wraps the
    indexed timestamp column in a DATE() transform MySQL can't use for
    an index range scan."""
    return timezone.make_aware(datetime.combine(target_date, time.min))


def _normalize_historical_row(model_label, history_row):
    """One HistoricalRecords() row -> a plain dict shaped like a
    PlatformSettingChange entry, so both sources can be merged and
    sorted together. Uses simple_history's own diff_against()/ModelDelta
    API for "what changed" -- not a hand-rolled diff.

    Known, accepted tradeoff: reading .prev_record issues one extra query
    per row (an N+1 shape) -- acceptable for a bounded, paginated
    admin-only screen, not something this task engineers a prefetch
    solution for."""
    if history_row.history_type == "+":
        changed_fields = ["Created"]
    elif history_row.history_type == "-":
        changed_fields = ["Deleted"]
    else:
        # A property, not a method -- simple_history exposes these as
        # `prev_record`/`next_record` (django-simple-history's own
        # get_extra_fields() attaches them via `property(get_next_record)`),
        # despite the underlying function being named get_prev_record.
        prev = history_row.prev_record
        if prev:
            # "Field Label: old → new" per changed field, matching
            # PlatformSettingChange's own "old → new" display shape
            # below -- .changes (not just .changed_fields) carries the
            # actual old/new values, not just which fields moved.
            # humanize_identifier_name turns the raw field name (e.g.
            # "kyc_status") into something a non-technical admin can
            # read ("KYC Status") -- a real usability gap the user
            # caught live on the screen before this fix.
            changed_fields = [
                f"{humanize_identifier_name(change.field)}: {change.old} → {change.new}"
                for change in history_row.diff_against(prev).changes
            ]
        else:
            changed_fields = []
    return {
        "model": _MODEL_DISPLAY_LABELS.get(model_label, model_label),
        "object_repr": _safe_object_repr(model_label, history_row),
        "action": history_row.get_history_type_display(),
        "actor": _actor_display(history_row.history_user),
        "timestamp": history_row.history_date,
        "changed_fields": changed_fields,
    }


def _humanize_object_repr(model_label, obj):
    """A friendlier display than a model's own debug-style __str__ for
    the models where one exists (Distributor.__str__ -- "Distributor<+
    233...>", Order.__str__ -- "Order<63 confirmed>") -- caught live by
    the user reviewing a real screenshot of this screen. Deliberately
    local to the audit log rather than changing those __str__ methods:
    apps.admin_portal.views already has an established precedent of
    computing a proper display name locally instead of fixing
    Distributor.__str__ at the source (kyc_review_detail,
    distributor_profile -- both have tests asserting the raw
    "Distributor<" repr never reaches rendered output), to avoid an
    unaudited blast radius from changing a __str__ potentially relied on
    elsewhere (Django Admin, log lines). This follows that same
    precedent for Order/WithdrawalRequest too, for consistency. Every
    other tracked model's own __str__ is already a plain, readable name
    (Category/Product/NotificationTemplate) and needs no override."""
    if model_label == "Distributor":
        name = getattr(obj, "full_name", "") or str(
            getattr(obj, "phone_number", "") or ""
        )
        return name or f"Distributor #{obj.pk}"
    if model_label == "Order":
        return f"Order #{obj.pk} ({obj.get_status_display()})"
    if model_label == "WithdrawalRequest":
        return f"Withdrawal #{obj.pk} ({obj.get_status_display()})"
    return str(obj)


def _actor_display(user):
    """A plain string, not the raw User instance -- Task 48's click-to-
    open detail modal passes each entry through Django's json_script
    filter (DjangoJSONEncoder), which has no built-in support for
    serializing an arbitrary model instance. Baking the "System" default
    in here (rather than a template-side `|default:"System"`) means
    every consumer -- the table cell and the modal alike -- always sees
    the same already-resolved string, with one source of truth for it."""
    return str(user) if user else "System"


def _safe_object_repr(model_label, history_row):
    """A real bug caught live (not in a test) while first exercising this
    function: history_object reconstructs a plain instance from THIS
    row's own field values, but calling str() on it can still trigger a
    FRESH, LIVE FK lookup (e.g. the old, unhumanized Distributor.__str__
    read self.user, Django's related-object descriptor re-querying User
    by the stored user_id rather than using any cached value) -- and
    raises User.DoesNotExist if that related row was deleted since this
    historical snapshot was taken. An audit-log row surviving the
    deletion of what it references is the whole point of this table
    (same reasoning as OrderCycleFailure/CommissionCycleFailure's
    plain-int FKs) -- it must never crash the WHOLE screen because of
    one now-dangling reference on one row. _humanize_object_repr's
    Distributor branch reads full_name/phone_number (plain fields
    already in the historical snapshot, no live query) instead of
    Distributor.__str__, which incidentally also closes off the
    original failure mode for that model specifically -- this try/except
    remains the general-purpose safety net for every other model."""
    try:
        return _humanize_object_repr(model_label, history_row.history_object)
    except Exception:
        pk = getattr(history_row.history_object, "pk", "?")
        return f"{model_label} #{pk} (related record deleted)"


def get_unified_audit_log(*, date_from=None, date_to=None):
    """Task 47e. Merges every HistoricalRecords()-tracked model's history
    with PlatformSettingChange (Task 47d's own bespoke audit log for
    constance settings -- not HistoricalRecords()-based, but the same
    audit-log initiative per SPEC_PHASE2.md's own framing) into one
    timestamp-sorted list.

    Each source query is independently bounded by date_from/date_to
    (never unbounded) before merging in Python -- this project's own
    established "sum/merge a small, already-bounded dataset in memory
    rather than a cross-model SQL UNION" precedent
    (apps.reporting.services.bucket_revenue_series does the same for a
    single model's rollup rows). Defaults to the trailing 30 days when
    no range is given, mirroring apps.admin_portal.views
    ._sales_revenue_report_params's own default-range reasoning."""
    today = timezone.now().date()
    if date_from is None:
        date_from = today - timedelta(days=29)
    if date_to is None:
        date_to = today
    start_at, end_at = _day_start(date_from), _day_start(date_to + timedelta(days=1))

    entries = []

    changes = PlatformSettingChange.objects.filter(
        changed_at__gte=start_at, changed_at__lt=end_at
    )
    for change in changes:
        entries.append(
            {
                "model": "Platform Setting",
                "object_repr": humanize_identifier_name(change.key),
                "action": "Changed",
                "actor": _actor_display(change.changed_by),
                "timestamp": change.changed_at,
                "changed_fields": [f"{change.old_value} → {change.new_value}"],
            }
        )

    for label, model in _HISTORY_TRACKED_MODELS.items():
        rows = model.history.filter(
            history_date__gte=start_at, history_date__lt=end_at
        ).select_related("history_user")
        for row in rows:
            entries.append(_normalize_historical_row(label, row))

    entries.sort(key=lambda entry: entry["timestamp"], reverse=True)
    return entries
