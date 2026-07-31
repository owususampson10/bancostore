import csv
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Prefetch, ProtectedError, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date

from constance import config
from constance.utils import get_values

from apps.catalog.models import Category, Product
from apps.catalog.services import normalize_primary_image
from apps.commissions.models import CommissionCycleRun
from apps.distributors.models import Distributor
from apps.distributors.services import approve_kyc, reject_kyc
from apps.orders.models import Order, OrderItem
from apps.orders.services import (
    ADVANCEABLE_STATUSES,
    advance_order_status,
    cancel_or_refund_order,
    is_legal_order_status_transition,
)
from apps.platform_settings.admin import BancostoreConstanceForm
from apps.platform_settings.config import CONSTANCE_CONFIG, CONSTANCE_CONFIG_FIELDSETS
from apps.wallet.models import WalletTransaction
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    InsufficientWalletBalance,
    KycNotApproved,
    PayoutDestinationNotSet,
    PayoutRecipientNameNotSet,
    WithdrawalRequestNotFound,
    WithdrawalRequestNotPending,
    approve_withdrawal_request,
    reject_withdrawal_request,
)

from .forms import (
    _INPUT_CLASS,
    _SELECT_CLASS,
    MAX_PRODUCT_IMAGES,
    CategoryForm,
    ProductForm,
    ProductVariantFormSet,
    build_product_image_formset,
)
from .permissions import is_admin_portal_staff

# Task 23. Mirrors apps.withdrawal.admin's own _APPROVE_FAILURE_MESSAGES
# exactly -- same exceptions, same admin-facing text, just surfaced via
# Django's messages framework instead of the bulk-action results summary.
_APPROVE_FAILURE_MESSAGES = {
    KycNotApproved: "KYC is no longer approved.",
    PayoutDestinationNotSet: "Payout destination is no longer set.",
    # PR #19 CodeRabbit finding, mirrored from apps.withdrawal.admin.
    PayoutRecipientNameNotSet: "Distributor has no name available for payout.",
    InsufficientWalletBalance: "Wallet balance is now insufficient.",
    WithdrawalRequestNotFound: "This request no longer exists.",
    WithdrawalRequestNotPending: "This request has already been handled.",
}

# code-review finding: was two copy-pasted {% if %}/{% elif %} blocks in
# commission_oversight.html and commission_cycle_detail.html -- a single
# source of truth here means a third job type only needs one edit, not a
# hunt through every template that happens to render a job_name.
_JOB_NAME_LABELS = {
    "calculate-binary-bonus": "Binary Bonus",
    "calculate-matching-bonus": "Matching Bonus",
}


# Same cutoff already used by partials/product_results.html's amber/red
# stock badges -- named here so the dashboard's count and the catalog
# table's own coloring can never silently drift apart.
LOW_STOCK_THRESHOLD = 10

# Orders that genuinely need an admin to move them forward. Pending is
# excluded on purpose -- it's unpaid and there's nothing to do until the
# customer pays or auto_cancel_unpaid_orders reaps it; delivered/
# cancelled/refunded are already done. Dispatched is excluded too -- it's
# in transit, waiting on the courier, not on an admin action.
ORDERS_AWAITING_ACTION_STATUSES = (Order.Status.CONFIRMED, Order.Status.PROCESSING)

# WalletTransaction rows that represent an actual commission payout, for
# the dashboard's "This Week's Commissions" figure -- deliberately not
# withdrawal debits/reversals or cooling-off refunds, which move money
# for unrelated reasons and would inflate a number meant to answer "how
# much did the network earn this week."
COMMISSION_TRANSACTION_TYPES = (
    WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
    WalletTransaction.TransactionType.BINARY_BONUS,
    WalletTransaction.TransactionType.MATCHING_BONUS,
)


@login_required(login_url="two_factor:login")
def dashboard(request):
    """Task 27. Replaces the Task 22 placeholder ("you're logged in, KYC
    Review is the only real feature so far") now that every other
    admin_portal section has actually shipped. Every number here is a
    real query, not a fabricated stat -- built from a fetched Stitch
    screen but reconciled against real scope first: the mockup's global
    search bar, notification bell, settings gear, floating action
    button, "System Status" pill, and 7/30-day toggle were all dropped
    since none of them correspond to a feature that exists (same
    "reconcile against real scope" pass every other Stitch-sourced page
    in this codebase has gone through)."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    week_start = timezone.now() - timedelta(days=7)

    pending_kyc_count = Distributor.objects.filter(
        kyc_status=Distributor.KycStatus.PENDING,
        didit_verification__isnull=False,
    ).count()
    pending_withdrawals_count = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.Status.SUBMITTED
    ).count()
    orders_awaiting_action_count = Order.objects.filter(
        status__in=ORDERS_AWAITING_ACTION_STATUSES
    ).count()
    low_stock_count = Product.objects.filter(
        is_active=True, stock__lt=LOW_STOCK_THRESHOLD
    ).count()

    total_distributors = Distributor.objects.count()
    new_distributors_this_week = Distributor.objects.filter(
        user__date_joined__gte=week_start
    ).count()
    total_products = Product.objects.filter(is_active=True).count()

    orders_this_week = Order.objects.filter(created_at__gte=week_start)
    orders_this_week_count = orders_this_week.count()
    # Only orders that actually collected payment count toward the GHS
    # figure -- pending is unpaid, cancelled/refunded gave the money
    # back, neither is real revenue.
    orders_this_week_value = orders_this_week.exclude(
        status__in=[Order.Status.PENDING, Order.Status.CANCELLED, Order.Status.REFUNDED]
    ).aggregate(total=Sum("total"))["total"] or Decimal("0")

    commissions_this_week = WalletTransaction.objects.filter(
        transaction_type__in=COMMISSION_TRANSACTION_TYPES,
        created_at__gte=week_start,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    recent_orders = Order.objects.order_by("-created_at", "-pk")[:6]

    context = {
        "pending_kyc_count": pending_kyc_count,
        "pending_withdrawals_count": pending_withdrawals_count,
        "orders_awaiting_action_count": orders_awaiting_action_count,
        "low_stock_count": low_stock_count,
        "total_distributors": total_distributors,
        "new_distributors_this_week": new_distributors_this_week,
        "total_products": total_products,
        "orders_this_week_count": orders_this_week_count,
        "orders_this_week_value": orders_this_week_value,
        "commissions_this_week": commissions_this_week,
        "recent_orders": recent_orders,
        "active_nav": "dashboard",
    }
    return render(request, "admin_portal/dashboard.html", context)


@login_required(login_url="two_factor:login")
def kyc_review_queue(request):
    """Task 22. Branded replacement for DistributorAdmin's Django-Admin KYC
    list -- presentation only, no service-layer changes. Only distributors
    who actually have a submitted DiditVerification belong in a review
    queue; a pending distributor with none yet hasn't reached the KYC step
    and has nothing here for an admin to act on."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributors = (
        Distributor.objects.filter(
            kyc_status=Distributor.KycStatus.PENDING,
            didit_verification__isnull=False,
        )
        .select_related("user", "didit_verification")
        .order_by("didit_verification__created_at")
    )
    return render(
        request,
        "admin_portal/kyc_review_queue.html",
        {"distributors": distributors, "active_nav": "kyc_review"},
    )


@login_required(login_url="two_factor:login")
def kyc_review_detail(request, pk):
    """Task 22. GET shows one distributor's verification detail; POST
    approves or rejects via the exact same apps.distributors.services
    functions the Django-Admin bulk actions already call -- no new
    approval/rejection logic here, only a branded front end for it.

    code-review finding (2026-07-23): the queue already filters to
    distributors with a submitted DiditVerification, but this detail
    route is reachable directly by pk regardless -- without the same
    filter here, a staff user could POST approve against a distributor
    with no submission at all, and approve_kyc would happily assign an
    IR ID with nothing to actually review."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributor = get_object_or_404(
        Distributor.objects.select_related("user", "didit_verification"),
        pk=pk,
        didit_verification__isnull=False,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            approve_kyc(distributor)
            messages.success(request, f"Approved KYC for {distributor}.")
            return redirect("admin_portal:kyc_review_queue")
        if action == "reject":
            reason = request.POST.get("reason", "").strip()
            if not reason:
                messages.error(request, "A rejection reason is required.")
            else:
                reject_kyc(distributor, reason=reason)
                messages.success(request, f"Rejected KYC for {distributor}.")
                return redirect("admin_portal:kyc_review_queue")

    preset_reasons = [
        r.strip() for r in config.KYC_REJECTION_REASONS.split(",") if r.strip()
    ]
    verification = getattr(distributor, "didit_verification", None)
    doc_images = (
        [
            ("ID Front", verification.id_front_image),
            ("ID Back", verification.id_back_image),
            ("Selfie", verification.selfie_image),
        ]
        if verification
        else []
    )
    # Distributor.full_name is only ever set from PendingRegistration at
    # registration time -- a distributor who registered before that field
    # existed, or whose registration data is otherwise blank, still has a
    # name Didit extracted from their ID. Prefer the distributor's own
    # value (it's what they'll be addressed as elsewhere in the app) but
    # fall back to the extracted one rather than showing "no name" when a
    # real name is sitting right there in the verification.
    display_name = distributor.full_name or (
        verification.extracted_full_name if verification else ""
    )
    return render(
        request,
        "admin_portal/kyc_review_detail.html",
        {
            "distributor": distributor,
            "verification": verification,
            "doc_images": doc_images,
            "preset_reasons": preset_reasons,
            "display_name": display_name,
            "active_nav": "kyc_review",
        },
    )


@login_required(login_url="two_factor:login")
def withdrawal_review_queue(request):
    """Task 23. Branded replacement for WithdrawalRequestAdmin's Django-
    Admin changelist -- presentation only, no service-layer changes.
    Only status=SUBMITTED requests belong in a review queue; anything
    further along (approved/paid/rejected/reversed) has already been
    decided and has nothing left for an admin to act on here."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    withdrawal_requests = (
        WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.SUBMITTED)
        .select_related(
            "distributor", "distributor__user", "distributor__didit_verification"
        )
        .order_by("created_at")
    )
    # Real numbers, not the fabricated "42 pending / GHS 124.5k" stat
    # cards Stitch's own mockup invented -- a single aggregate query
    # rather than len()/sum() over the already-paginated-free queryset
    # above, so this stays one query regardless of queue size.
    totals = withdrawal_requests.aggregate(
        pending_count=Count("pk"), total_net_payout=Sum("net_amount")
    )
    # "High priority" is a display-only nudge, not an approval gate --
    # this codebase has no multi-tier admin permission system, so unlike
    # the Stitch mockup's "require senior admin approval" copy, this
    # never claims one exists.
    high_priority_count = withdrawal_requests.filter(
        amount__gt=config.HIGH_PRIORITY_WITHDRAWAL_THRESHOLD
    ).count()
    return render(
        request,
        "admin_portal/withdrawal_review_queue.html",
        {
            "withdrawal_requests": withdrawal_requests,
            "pending_count": totals["pending_count"],
            "total_net_payout": totals["total_net_payout"] or Decimal("0"),
            "high_priority_count": high_priority_count,
            "high_priority_threshold": config.HIGH_PRIORITY_WITHDRAWAL_THRESHOLD,
            "active_nav": "withdrawals",
        },
    )


@login_required(login_url="two_factor:login")
def withdrawal_review_detail(request, pk):
    """Task 23. GET shows one request's review detail; POST approves or
    rejects via the exact same apps.withdrawal.services functions the
    Django-Admin bulk actions already call -- no new approval/rejection
    logic here, only a branded front end for it.

    Mirrors kyc_review_detail's own direct-URL-bypass fix (Task 22 code
    review): scoping the lookup to status=SUBMITTED so a staff user can't
    POST approve/reject against a request that's already been decided --
    the service functions would reject that too (WithdrawalRequestNotPending),
    but failing here with a clean 404 is more honest than reaching the
    service layer just to get bounced."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    withdrawal_request = get_object_or_404(
        WithdrawalRequest.objects.select_related(
            "distributor", "distributor__user", "distributor__didit_verification"
        ),
        pk=pk,
        status=WithdrawalRequest.Status.SUBMITTED,
    )
    distributor = withdrawal_request.distributor
    # Same fallback as kyc_review_detail: Distributor.full_name is only
    # ever set from PendingRegistration at registration time, so a
    # distributor with a blank one still has a name Didit extracted from
    # their ID -- and every distributor reaching this screen has an
    # approved DiditVerification by definition (KYC-gated at submission).
    # Computed up front (code-review finding) so both the flash messages
    # below and the page itself use the same real name instead of
    # Distributor.__str__'s "Distributor<+233...>" debug repr.
    verification = getattr(distributor, "didit_verification", None)
    display_name = distributor.full_name or (
        verification.extracted_full_name if verification else ""
    )
    distributor_label = display_name or distributor

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            try:
                approve_withdrawal_request(withdrawal_request, reviewed_by=request.user)
                messages.success(
                    request, f"Approved withdrawal request for {distributor_label}."
                )
                return redirect("admin_portal:withdrawal_review_queue")
            except tuple(_APPROVE_FAILURE_MESSAGES) as exc:
                messages.error(request, _APPROVE_FAILURE_MESSAGES[type(exc)])
        elif action == "reject":
            reason = request.POST.get("reason", "").strip()
            if not reason:
                messages.error(request, "A rejection reason is required.")
            else:
                try:
                    reject_withdrawal_request(
                        withdrawal_request, reviewed_by=request.user, reason=reason
                    )
                    messages.success(
                        request,
                        f"Rejected withdrawal request for {distributor_label}.",
                    )
                    return redirect("admin_portal:withdrawal_review_queue")
                except (WithdrawalRequestNotFound, WithdrawalRequestNotPending) as exc:
                    messages.error(request, _APPROVE_FAILURE_MESSAGES[type(exc)])

    # WithdrawalRequest.payout_mobile_money_number/network are only
    # populated at approval time (ADR-0004 point 7) -- blank here since
    # this request is still status=SUBMITTED. The live Distributor fields
    # are what approval will actually snapshot and pay out to.
    wallet = getattr(distributor, "wallet", None)
    wallet_balance = wallet.balance if wallet else Decimal("0")
    # tax_amount/amount is reconstructed per-request rather than reading
    # the live WITHHOLDING_TAX_RATE constance setting -- amount/tax_amount
    # were locked in at submission (ADR-0004 point 10), so this shows the
    # rate actually applied to *this* request even if the live setting has
    # since changed, instead of a value that could go stale or mislead.
    tax_rate_percent = (
        (withdrawal_request.tax_amount / withdrawal_request.amount * 100)
        if withdrawal_request.amount
        else Decimal("0")
    )
    return render(
        request,
        "admin_portal/withdrawal_review_detail.html",
        {
            "withdrawal_request": withdrawal_request,
            "distributor": distributor,
            "wallet_balance": wallet_balance,
            "display_name": display_name,
            "tax_rate_percent": tax_rate_percent,
            # code-review finding: the approve modal's copy previously
            # hardcoded "Friday" -- WITHDRAWAL_DAY is a live,
            # admin-editable setting (ADR-0004), so this must track it.
            "withdrawal_day": config.WITHDRAWAL_DAY.capitalize(),
            "active_nav": "withdrawals",
        },
    )


def _filtered_distributors(request):
    """Shared by the directory page and the CSV export, so both search the
    same way and an exported file always matches what's on screen."""
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()

    # "pk" is a tie-breaker: full_name isn't unique (including two blank
    # names), and without one, Paginator can skip or duplicate a row across
    # a page boundary -- the exact bug class already caught in Task 15's
    # earnings-history pagination.
    distributors = Distributor.objects.select_related("user").order_by(
        "full_name", "pk"
    )
    if query:
        # Distributors are stored E.164 (+233...), but an admin naturally
        # types the local "0..." format -- rewrite so both find the same
        # result. A bare local number with no prefix already matches via
        # the icontains substring check below without any rewriting.
        phone_query = query
        if phone_query.startswith("0") and phone_query[1:].isdigit():
            phone_query = "+233" + phone_query[1:]
        distributors = distributors.filter(
            Q(full_name__icontains=query)
            | Q(ir_id__icontains=query)
            | Q(phone_number__icontains=phone_query)
        )
    if status == "active":
        distributors = distributors.filter(user__is_active=True)
    elif status == "suspended":
        distributors = distributors.filter(user__is_active=False)

    return distributors, query, status


@login_required(login_url="two_factor:login")
def distributor_directory(request):
    """Task 23. Branded replacement for DistributorAdmin's Django-Admin
    changelist search -- presentation only, no service-layer changes.
    Unlike the KYC/Withdrawal queues (small, naturally bounded to
    pending items), this directory can hold every distributor on the
    platform, so it gets real search, an account-status filter, and real
    pagination rather than showing everything on one page.

    Search and filter are real-time (Task 23 follow-up): mirrors
    apps.catalog.views.product_list's own hx-trigger pattern exactly --
    an htmx GET on keyup/change, no Apply button, swapping only the
    results partial so the rest of the page (including the performance
    insights footer) doesn't re-render on every keystroke."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributors, query, status = _filtered_distributors(request)

    paginator = Paginator(distributors, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    params = request.GET.copy()
    params.pop("page", None)
    querystring_no_page = params.urlencode()

    context = {
        "page_obj": page_obj,
        "query": query,
        "status": status,
        "querystring_no_page": querystring_no_page,
        "active_nav": "distributors",
    }

    if request.htmx:
        return render(request, "admin_portal/partials/directory_results.html", context)

    # These are platform-wide totals, not scoped to the current search/
    # filter (the Stitch design's own "1,248 / 42 / 92%" numbers read as
    # global stats, not filtered ones) -- computed once on full-page load
    # only, not on every htmx keystroke request.
    total_distributors = Distributor.objects.count()
    new_this_week = Distributor.objects.filter(
        user__date_joined__gte=timezone.now() - timedelta(days=7)
    ).count()
    # "Approval rate" is of decisions actually made (approved or rejected),
    # not diluted by distributors who haven't reached KYC review yet --
    # otherwise the rate would look artificially low and wouldn't answer
    # the question "of the ones we've reviewed, how many do we approve."
    decided = Distributor.objects.filter(
        kyc_status__in=[
            Distributor.KycStatus.APPROVED,
            Distributor.KycStatus.REJECTED,
        ]
    )
    decided_count = decided.count()
    kyc_approval_rate = (
        round(
            decided.filter(kyc_status=Distributor.KycStatus.APPROVED).count()
            / decided_count
            * 100
        )
        if decided_count
        else None
    )
    context.update(
        {
            "total_distributors": total_distributors,
            "new_this_week": new_this_week,
            "kyc_approval_rate": kyc_approval_rate,
        }
    )
    return render(request, "admin_portal/distributor_directory.html", context)


def _csv_safe(value):
    """Neutralizes CSV formula injection (OWASP): full_name is free text a
    distributor sets themselves at registration, and phone_number is
    E.164 (always starts with "+") -- this file is one a staff admin will
    realistically open in Excel/Sheets, and either would otherwise execute
    or misparse as a formula on open. Checked after stripping leading
    whitespace (a formula can be padded to dodge a naive startswith check)
    but the quote is prefixed to the original value so nothing is lost."""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@login_required(login_url="two_factor:login")
def distributor_directory_export(request):
    """Task 23 follow-up. Exports exactly what the current search/filter
    shows (via the same _filtered_distributors as the page itself), not
    the whole table unconditionally -- an admin who searched down to one
    distributor and clicks Export should get that one row, not all 1,248."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributors, _query, _status = _filtered_distributors(request)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="distributors.csv"'
    writer = csv.writer(response)
    writer.writerow(
        ["Full Name", "IR ID", "Phone Number", "Rank", "KYC Status", "Account Status"]
    )
    for distributor in distributors:
        writer.writerow(
            [
                _csv_safe(distributor.full_name),
                distributor.ir_id or "",
                _csv_safe(distributor.phone_number),
                distributor.rank,
                distributor.get_kyc_status_display(),
                "Active" if distributor.user.is_active else "Suspended",
            ]
        )
    return response


@login_required(login_url="two_factor:login")
def distributor_profile(request, pk):
    """Task 23. GET shows one distributor's read-only profile; POST
    toggles User.is_active -- confirmed via reading apps.distributors.
    backends.PhoneNumberBackend that this genuinely blocks login through
    Django's own ModelBackend.user_can_authenticate(), not a cosmetic
    flag. No other field is editable from this screen (per the design
    brief: a read-only profile plus this one action, not a general edit
    form)."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributor = get_object_or_404(
        Distributor.objects.select_related("user", "sponsor", "didit_verification"),
        pk=pk,
    )
    # Same fallback as kyc_review_detail/withdrawal_review_detail: a
    # distributor with a blank full_name still has a name Didit extracted
    # from their ID, if they've gone through KYC. Computed once and reused
    # by the template and the flash message so neither ever falls back to
    # Distributor.__str__'s "Distributor<+233...>" debug repr.
    verification = getattr(distributor, "didit_verification", None)
    display_name = distributor.full_name or (
        verification.extracted_full_name if verification else ""
    )
    distributor_label = display_name or distributor

    if request.method == "POST" and request.POST.get("action") == "toggle_active":
        distributor.user.is_active = not distributor.user.is_active
        distributor.user.save(update_fields=["is_active"])
        verb = "Reactivated" if distributor.user.is_active else "Suspended"
        messages.success(request, f"{verb} {distributor_label}'s account.")
        return redirect("admin_portal:distributor_profile", pk=distributor.pk)

    wallet = getattr(distributor, "wallet", None)
    wallet_balance = wallet.balance if wallet else Decimal("0")
    # starter_pack_choice is a raw "A"/"B" code -- apps.platform_settings
    # .config only defines STARTER_PACK_A/B_PRICE/PV/RANK, no display
    # name, so "Pack A"/"Pack B" is the honest label, not a fabricated
    # marketing name like Stitch's own mockup used.
    starter_pack_label = (
        f"Pack {distributor.starter_pack_choice}"
        if distributor.starter_pack_choice
        else ""
    )
    return render(
        request,
        "admin_portal/distributor_profile.html",
        {
            "distributor": distributor,
            "display_name": display_name,
            "wallet_balance": wallet_balance,
            "starter_pack_label": starter_pack_label,
            "active_nav": "distributors",
        },
    )


@login_required(login_url="two_factor:login")
def commission_oversight(request):
    """Task 23 (final piece). Read-only observability over the two
    Celery-Beat-automated bonus jobs (Binary Bonus, Matching Bonus) --
    presentation only, no service-layer changes. No manual-trigger action
    here: no such service function exists today, and building one is new
    scope needing its own locking/idempotency review (the same kind Task
    13's batch driver already went through).

    Direct Referral Bonus is credited instantly at purchase time by
    apps.commissions.services, not by a batch job, so it has no cycle-run
    history -- only a lifetime total, same as the other two."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    # code-review finding: was three separate aggregate queries (one
    # per transaction_type); a single conditional aggregate matches the
    # one-query convention withdrawal_review_queue already established
    # for its own summary cards.
    totals = WalletTransaction.objects.aggregate(
        direct_referral=Sum(
            "amount",
            filter=Q(
                transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS
            ),
        ),
        binary_bonus=Sum(
            "amount",
            filter=Q(transaction_type=WalletTransaction.TransactionType.BINARY_BONUS),
        ),
        matching_bonus=Sum(
            "amount",
            filter=Q(transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS),
        ),
    )

    # "-pk" tie-breaker: Binary Bonus (every 10 min) and Matching Bonus
    # (every N days) run on independent schedules that could coincide on
    # the same run_at -- without a tie-breaker, Paginator can skip or
    # duplicate a row across a page boundary, the same bug class already
    # fixed twice in the Distributor Directory this session.
    cycle_runs = CommissionCycleRun.objects.order_by("-run_at", "-pk")
    paginator = Paginator(cycle_runs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    for run in page_obj.object_list:
        run.job_label = _JOB_NAME_LABELS.get(run.job_name, run.job_name)

    return render(
        request,
        "admin_portal/commission_oversight.html",
        {
            "page_obj": page_obj,
            "total_direct_referral": totals["direct_referral"] or Decimal("0"),
            "total_binary_bonus": totals["binary_bonus"] or Decimal("0"),
            "total_matching_bonus": totals["matching_bonus"] or Decimal("0"),
            "active_nav": "commissions",
        },
    )


@login_required(login_url="two_factor:login")
def commission_cycle_detail(request, pk):
    """Task 23 (final piece). One batch cycle's failure log -- read-only,
    no retry/resolve action, mirroring CommissionCycleFailure's own design
    intent (an audit record that must survive even a deleted distributor
    row, not a workflow item with resolvable state)."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    cycle_run = get_object_or_404(CommissionCycleRun, pk=pk)
    cycle_run.job_label = _JOB_NAME_LABELS.get(cycle_run.job_name, cycle_run.job_name)
    failures = [
        {
            "distributor_id": failure.distributor_id,
            "error": failure.error,
            "created_at": failure.created_at,
            "elapsed_seconds": (failure.created_at - cycle_run.run_at).total_seconds(),
        }
        for failure in cycle_run.failures.all()
    ]

    return render(
        request,
        "admin_portal/commission_cycle_detail.html",
        {
            "cycle_run": cycle_run,
            "failures": failures,
            "active_nav": "commissions",
        },
    )


def _filtered_orders(request):
    """Shared filter logic for the order management queue -- mirrors
    _filtered_distributors's exact shape: search + status filter, plus a
    stable pk tie-breaker in ordering (Task 15's own pagination-bug
    precedent -- created_at alone isn't guaranteed unique, especially
    across orders confirmed in the same batch/second)."""
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    date_from = parse_date(request.GET.get("date_from", "").strip())
    date_to = parse_date(request.GET.get("date_to", "").strip())

    orders = Order.objects.select_related("customer").order_by("-created_at", "-pk")
    if query:
        orders = orders.filter(
            Q(full_name__icontains=query)
            | Q(phone_number__icontains=query)
            | Q(payment_reference__icontains=query)
        )
    if status:
        orders = orders.filter(status=status)
    if date_from:
        orders = orders.filter(created_at__date__gte=date_from)
    if date_to:
        orders = orders.filter(created_at__date__lte=date_to)

    return orders, query, status, date_from, date_to


@login_required(login_url="two_factor:login")
def order_management_queue(request):
    """Task 18e. Backend for the admin order management page -- Task 18f
    builds the real Stitch-designed template this renders into; this
    view owns filtering, pagination, and nothing else. Cancel/refund/
    status-update actions live in order_management_action below, wired
    directly to apps.orders.services's own functions -- never
    reimplementing their transition/reversal logic here."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    orders, query, status, date_from, date_to = _filtered_orders(request)

    paginator = Paginator(orders, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Task 18f: mirrors distributor_directory's own querystring_no_page --
    # hand-interpolating q/status/date_from/date_to into each pagination
    # link's href (as the very first draft of this template did) breaks
    # the moment a search term contains "&" (HTML-unescapes back into a
    # second bare query param, silently corrupting the filter) instead of
    # being properly URL-encoded.
    params = request.GET.copy()
    params.pop("page", None)
    querystring_no_page = params.urlencode()

    context = {
        "page_obj": page_obj,
        "query": query,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "querystring_no_page": querystring_no_page,
        "status_choices": Order.Status.choices,
        "active_nav": "orders",
    }
    # Real-time filtering (search keyup / status pick / date pick, no
    # Filter button): a change-triggered htmx request must get back just
    # the results fragment, not the full page shell, or the sidebar/
    # header would get swapped into the results div -- mirrors
    # distributor_directory's own request.htmx branch exactly.
    if request.htmx:
        return render(request, "admin_portal/partials/order_results.html", context)
    return render(request, "admin_portal/order_management_queue.html", context)


@login_required(login_url="two_factor:login")
def order_detail(request, pk):
    """Task 18f. GET-only single-order detail view, the page
    order_management_action redirects back to after every action so an
    admin's cancel/refund/advance/tracking-note update is visible in
    place -- matching the Stitch "Order Detail" screen's own in-place
    flash-message design, not a bounce back to the queue. Mirrors
    order_invoice_pdf's own select_related/prefetch_related shape.

    advanceable_statuses/can_cancel/can_refund are all computed via
    is_legal_order_status_transition (18a's single source of truth) --
    never a second, hand-maintained copy of _ALLOWED_TRANSITIONS here."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    # Prefetch, not a plain "items__product__images" string: a single
    # Order lookup already prefetches items -> product in one extra
    # query, but product.primary_image (rendered per line in the
    # template) queries product.images.all() again per item with no
    # prefetch -- the same N+1 apps.orders.cart.Cart.items() already
    # documents and fixes for the storefront cart page.
    order = get_object_or_404(
        Order.objects.select_related("customer").prefetch_related(
            Prefetch(
                "items",
                queryset=OrderItem.objects.select_related(
                    "product__category"
                ).prefetch_related("product__images"),
            )
        ),
        pk=pk,
    )
    advanceable_statuses = [
        (status, status.label)
        for status in ADVANCEABLE_STATUSES
        if is_legal_order_status_transition(order.status, status)
    ]
    return render(
        request,
        "admin_portal/order_detail.html",
        {
            "order": order,
            "advanceable_statuses": advanceable_statuses,
            # Excludes PENDING explicitly, not just via
            # is_legal_order_status_transition: that graph legitimately
            # allows PENDING -> CANCELLED (Task 17c/17d's own automatic
            # pre-payment cancellation, Task 18d's auto-cancel batch job),
            # but cancel_or_refund_order (18b) documents PENDING as a
            # caller bug -- it silently no-ops rather than raising, so
            # without this exclusion the button would show "cancelled"
            # while doing nothing. Found via real-browser verification,
            # 2026-07-27.
            "can_cancel": order.status != Order.Status.PENDING
            and is_legal_order_status_transition(order.status, Order.Status.CANCELLED),
            "can_refund": is_legal_order_status_transition(
                order.status, Order.Status.REFUNDED
            ),
            "active_nav": "orders",
        },
    )


def _friendly_service_error(exc: Exception) -> str:
    """apps.orders.services's own exception messages are prefixed with
    the raising function's name (e.g. "cancel_or_refund_order: ..."),
    a useful detail in logs but not admin-facing copy -- security-and-
    hardening review (2026-07-26): the underlying reason is still fine
    to show this specific audience (is_admin_portal_staff-gated, trusted
    staff, not a public endpoint), only the internal-function-name
    prefix needs stripping. A RuntimeError specifically only ever means
    an illegal transition here (18a's tripwire) -- a fixed, friendlier
    message reads better than the raw "Illegal order status transition:
    ..." text."""
    if isinstance(exc, RuntimeError):
        return "That status change isn't allowed from this order's current status."
    message = str(exc)
    return message.split(": ", 1)[1] if ": " in message else message


@login_required(login_url="two_factor:login")
def order_management_action(request, pk):
    """Task 18e. POST-only action endpoint: translates a form payload
    into the right apps.orders.services call and turns its exceptions
    into a flash message, exactly matching withdrawal_review_detail's
    own established shape. `to_status`/`restock` validation and the
    transition-legality check all live in the service functions
    themselves (18a/18b/18c) -- this view never duplicates them."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    order = get_object_or_404(Order, pk=pk)
    action = request.POST.get("action")

    if action == "cancel":
        try:
            cancel_or_refund_order(order.pk, Order.Status.CANCELLED)
            messages.success(request, f"Order {order.payment_reference} cancelled.")
        except (ValueError, RuntimeError) as exc:
            messages.error(request, _friendly_service_error(exc))
    elif action == "refund":
        restock_raw = request.POST.get("restock")
        if restock_raw not in ("true", "false"):
            messages.error(
                request, "Choose whether the goods were physically returned."
            )
        else:
            try:
                cancel_or_refund_order(
                    order.pk,
                    Order.Status.REFUNDED,
                    restock=(restock_raw == "true"),
                )
                messages.success(request, f"Order {order.payment_reference} refunded.")
            except (ValueError, RuntimeError) as exc:
                messages.error(request, _friendly_service_error(exc))
    elif action == "advance":
        to_status = request.POST.get("to_status", "")
        tracking_note = request.POST.get("tracking_note", "")
        # CodeRabbit (2026-07-27): the "Save Changes" button that submits
        # this form lives in the page header, far from the Operational
        # State radios -- a native HTML5 required-radio validation bubble
        # anchored there was confusing, so the radios no longer carry
        # `required` (templates/admin_portal/order_detail.html). Without
        # this check, a blank to_status would instead reach
        # advance_order_status's own ValueError, whose message embeds the
        # raw Order.Status enum repr -- not admin-facing text. Mirrors the
        # "refund" branch's own restock_raw check just above.
        if not to_status:
            messages.error(request, "Choose a status to advance to.")
            return redirect("admin_portal:order_detail", pk=order.pk)
        try:
            advance_order_status(order.pk, to_status, tracking_note=tracking_note)
            messages.success(
                request,
                f"Order {order.payment_reference} moved to "
                f"{Order.Status(to_status).label}.",
            )
        except (ValueError, RuntimeError) as exc:
            messages.error(request, _friendly_service_error(exc))
    else:
        messages.error(request, "Unrecognized action.")

    # Task 18f: redirects to this same order's detail page, not the queue
    # -- the Stitch "Order Detail" screen's own design keeps the admin on
    # the order they just acted on so the flash message and its new
    # status/history are immediately visible, not lost in a list.
    return redirect("admin_portal:order_detail", pk=order.pk)


@login_required(login_url="two_factor:login")
def order_invoice_pdf(request, pk):
    """Task 18e (ADR-0006 decision 7). Renders a plain-receipt PDF
    invoice for one order -- Order ID, customer name, items ordered,
    delivery method/address, total, payment status. No GRA withholding-
    tax logic (that's withdrawal-specific, per the ADR).

    WeasyPrint is imported lazily, inside this function, not at module
    level: its own __init__ eagerly dlopen()s the system Pango library
    at import time (confirmed directly by running it -- source-driven-
    development, 2026-07-26), and this project's local dev Mac has no
    Pango installed -- `brew install weasyprint` was attempted and
    failed after ~50 minutes (macOS 12 is an unsupported Homebrew
    Tier-3 configuration; see project_weasyprint_pango_blocked_locally
    memory). Verified for real in CI instead, where Pango installs
    cleanly via `apt` (.github/workflows/ci.yml). A module-level import
    here would break every OTHER view in this file on any machine
    missing Pango -- a lazy import confines that failure to this one
    endpoint. API confirmed against WeasyPrint's own docs: `HTML(string=
    ...).write_pdf()` returns PDF bytes with no arguments.
    Source: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html"""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    order = get_object_or_404(
        Order.objects.select_related("customer").prefetch_related("items"), pk=pk
    )
    html_string = render_to_string("admin_portal/order_invoice.html", {"order": order})

    from weasyprint import HTML

    pdf_bytes = HTML(string=html_string).write_pdf()

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="invoice-{order.payment_reference}.pdf"'
    )
    return response


# ---------------------------------------------------------------------------
# Catalog Management (Task 26) -- Category CRUD
# ---------------------------------------------------------------------------


def _filtered_categories(request):
    query = request.GET.get("q", "").strip()
    categories = Category.objects.annotate(product_count=Count("products"))
    if query:
        categories = categories.filter(
            Q(name__icontains=query) | Q(slug__icontains=query)
        )
    return categories.order_by("name"), query


@login_required(login_url="two_factor:login")
def catalog_category_list(request):
    """Branded replacement for Django Admin's CategoryAdmin changelist --
    presentation only, Category itself is unchanged. Real-time search (no
    Apply button), mirroring distributor_directory's own htmx pattern."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    categories, query = _filtered_categories(request)
    paginator = Paginator(categories, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)
    querystring_no_page = params.urlencode()

    context = {
        "page_obj": page_obj,
        "query": query,
        "querystring_no_page": querystring_no_page,
        "active_nav": "catalog",
    }
    if request.htmx:
        return render(request, "admin_portal/partials/category_results.html", context)
    return render(request, "admin_portal/catalog_category_list.html", context)


@login_required(login_url="two_factor:login")
def catalog_category_create(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    if request.method == "POST":
        form = CategoryForm(request.POST, request.FILES)
        if form.is_valid():
            category = form.save()
            messages.success(request, f'Category "{category.name}" created.')
            return redirect("admin_portal:catalog_category_list")
    else:
        form = CategoryForm()

    return render(
        request,
        "admin_portal/catalog_category_form.html",
        {"form": form, "is_edit": False, "active_nav": "catalog"},
    )


@login_required(login_url="two_factor:login")
def catalog_category_edit(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        form = CategoryForm(request.POST, request.FILES, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, f'Category "{category.name}" updated.')
            return redirect("admin_portal:catalog_category_list")
    else:
        form = CategoryForm(instance=category)

    return render(
        request,
        "admin_portal/catalog_category_form.html",
        {"form": form, "category": category, "is_edit": True, "active_nav": "catalog"},
    )


@login_required(login_url="two_factor:login")
def catalog_category_delete(request, pk):
    """POST-only -- Category.category (FK from Product) is on_delete=PROTECT
    (apps/catalog/models.py), so deleting a category that still has
    products raises ProtectedError. Caught here and turned into a plain
    flash message instead of a 500, the same shape as every other
    caught-service-exception-to-message pattern in this file."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        try:
            name = category.name
            category.delete()
            messages.success(request, f'Category "{name}" deleted.')
        except ProtectedError:
            messages.error(
                request,
                f'"{category.name}" still has products assigned to it and cannot '
                "be deleted. Move or delete its products first.",
            )
    return redirect("admin_portal:catalog_category_list")


# ---------------------------------------------------------------------------
# Catalog Management (Task 26) -- Product CRUD
# ---------------------------------------------------------------------------


def _filtered_products(request):
    query = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "").strip()
    status = request.GET.get("status", "").strip()
    featured = request.GET.get("featured", "").strip()
    low_stock = request.GET.get("low_stock", "").strip()

    products = Product.objects.select_related("category").prefetch_related("images")
    if query:
        products = products.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    # category_id is fully querystring-controlled -- Product.category_id is
    # an integer pk, and passing a non-numeric string straight into
    # .filter(category_id=...) raises ValueError (confirmed directly, not
    # guessed), an unhandled 500 for a crafted or simply stale link. Not
    # a real filter selection either way, so it's dropped rather than
    # surfaced as a form error.
    if category_id and category_id.isdigit():
        products = products.filter(category_id=category_id)
    if status == "active":
        products = products.filter(is_active=True)
    elif status == "inactive":
        products = products.filter(is_active=False)
    if featured == "yes":
        products = products.filter(is_featured=True)
    elif featured == "no":
        products = products.filter(is_featured=False)
    # Reached only via the dashboard's Low Stock Products card -- there's no
    # visible filter widget for this one, matching the "card links straight
    # to the matching subset" pattern the other 3 action-needed cards
    # already use (KYC/withdrawal/order queues).
    if low_stock == "1":
        products = products.filter(stock__lt=LOW_STOCK_THRESHOLD)

    return (
        products.order_by("-created_at", "-pk"),
        query,
        category_id,
        status,
        featured,
        low_stock,
    )


@login_required(login_url="two_factor:login")
def catalog_product_list(request):
    """Branded replacement for Django Admin's ProductAdmin changelist.
    Real-time search + category/status/featured filters (no Apply
    button), mirroring distributor_directory/order_management_queue's own
    htmx pattern exactly -- a change-triggered request gets back only the
    results partial, not the full page shell."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    products, query, category_id, status, featured, low_stock = _filtered_products(
        request
    )
    paginator = Paginator(products, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)
    querystring_no_page = params.urlencode()

    context = {
        "page_obj": page_obj,
        "query": query,
        "category_id": category_id,
        "status": status,
        "featured": featured,
        "low_stock": low_stock,
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "categories": Category.objects.order_by("name"),
        "querystring_no_page": querystring_no_page,
        "active_nav": "catalog",
    }
    if request.htmx:
        return render(request, "admin_portal/partials/product_results.html", context)
    return render(request, "admin_portal/catalog_product_list.html", context)


def _image_formset_extra(product):
    """How many blank image upload slots to render so existing + blank
    never exceeds MAX_PRODUCT_IMAGES -- a brand-new product (no images
    yet) gets all 5 slots, a product that already has images gets only
    however many are left."""
    existing_count = product.images.count() if product and product.pk else 0
    return max(0, MAX_PRODUCT_IMAGES - existing_count)


def _save_product_with_formsets(request, product):
    """Shared by create/edit: validates the product form plus both inline
    formsets together before saving anything, so an invalid variant row
    never leaves behind a half-saved product. Returns the saved product on
    success, or None (with all three forms left populated with errors for
    re-rendering) on failure."""
    form = ProductForm(request.POST, request.FILES, instance=product)
    # inlineformset_factory requires a real (even if unsaved) parent
    # instance -- Product() for create, the fetched row for edit.
    formset_parent = product or Product()
    image_formset_class = build_product_image_formset(_image_formset_extra(product))
    image_formset = image_formset_class(
        request.POST, request.FILES, instance=formset_parent, prefix="images"
    )
    variant_formset = ProductVariantFormSet(
        request.POST, instance=formset_parent, prefix="variants"
    )

    if form.is_valid() and image_formset.is_valid() and variant_formset.is_valid():
        with transaction.atomic():
            saved_product = form.save()
            image_formset.instance = saved_product
            image_formset.save()
            variant_formset.instance = saved_product
            variant_formset.save()
            normalize_primary_image(saved_product)
        return saved_product, form, image_formset, variant_formset

    return None, form, image_formset, variant_formset


@login_required(login_url="two_factor:login")
def catalog_product_create(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    if request.method == "POST":
        saved_product, form, image_formset, variant_formset = (
            _save_product_with_formsets(request, None)
        )
        if saved_product is not None:
            messages.success(request, f'Product "{saved_product.name}" created.')
            return redirect("admin_portal:catalog_product_list")
    else:
        form = ProductForm()
        image_formset_class = build_product_image_formset(MAX_PRODUCT_IMAGES)
        image_formset = image_formset_class(instance=Product(), prefix="images")
        variant_formset = ProductVariantFormSet(instance=Product(), prefix="variants")

    return render(
        request,
        "admin_portal/catalog_product_form.html",
        {
            "form": form,
            "image_formset": image_formset,
            "variant_formset": variant_formset,
            "categories": Category.objects.order_by("name"),
            "is_edit": False,
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def catalog_product_edit(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        saved_product, form, image_formset, variant_formset = (
            _save_product_with_formsets(request, product)
        )
        if saved_product is not None:
            messages.success(request, f'Product "{saved_product.name}" updated.')
            return redirect("admin_portal:catalog_product_list")
    else:
        form = ProductForm(instance=product)
        image_formset_class = build_product_image_formset(_image_formset_extra(product))
        image_formset = image_formset_class(instance=product, prefix="images")
        variant_formset = ProductVariantFormSet(instance=product, prefix="variants")

    return render(
        request,
        "admin_portal/catalog_product_form.html",
        {
            "form": form,
            "product": product,
            "image_formset": image_formset,
            "variant_formset": variant_formset,
            "categories": Category.objects.order_by("name"),
            "is_edit": True,
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def catalog_product_delete(request, pk):
    """POST-only -- OrderItem.product (FK from an order line) is
    on_delete=PROTECT (apps/orders/models.py: "a Product must not be
    deletable while order history still references it"), so deleting an
    already-ordered product raises ProtectedError. Caught here the same
    way catalog_category_delete already handles it, instead of a 500."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        try:
            name = product.name
            product.delete()
            messages.success(request, f'Product "{name}" deleted.')
        except ProtectedError:
            messages.error(
                request,
                f'"{product.name}" has already been ordered and cannot be '
                "deleted. Mark it inactive instead to hide it from the store.",
            )
    return redirect("admin_portal:catalog_product_list")


# ---------------------------------------------------------------------------
# Platform Settings (Task 28) -- a Stitch-designed front end for the
# already-existing django-constance business-rule settings, replacing raw
# Django Admin as the primary path (same pattern as every other admin_portal
# screen). Reuses apps.platform_settings.admin.BancostoreConstanceForm
# directly -- it's a plain forms.Form, not admin-specific -- rather than
# hand-rolling a parallel form that could drift from its already-tested
# field types/bounds/cross-field validation (e.g. the MIN/MAX withdrawal
# amount check, the percentage_field 0-100 bound).
# ---------------------------------------------------------------------------


# These 3 are genuinely long-form copy (legal text), unlike every other
# str-typed setting here (an email, a phone number, a hex colour) -- given
# a taller textarea so they're actually usable, not the same cramped
# 2-row box as a one-line value.
_LONG_TEXT_SETTINGS = {
    "TERMS_AND_CONDITIONS_TEXT",
    "PRIVACY_POLICY_TEXT",
    "REFUND_RETURN_POLICY_TEXT",
}

# One icon per CONSTANCE_CONFIG_FIELDSETS group, shown on the vertical-tab
# sidebar only (desktop, >=lg) -- not on the horizontal scroll strip the
# same tabs collapse to below that, per explicit user request.
_GROUP_ICONS = {
    "Authentication Settings": "lock_open",
    "Commission & Bonus Settings": "payments",
    "Registration & Membership Settings": "person_add",
    "Withdrawal & Payout Settings": "account_balance",
    "Delivery Settings": "local_shipping",
    "Order Settings": "receipt_long",
    "KYC Settings": "fact_check",
    "IR ID Number Settings": "badge",
    "Payment Gateway Settings": "point_of_sale",
    "General Platform Settings": "settings_suggest",
}

# A plain str.title() on an underscore-joined constant name reads fine for
# ordinary words ("Distributor Login Method") but mangles the acronyms
# scattered through this file's own setting names ("Otp", "Kyc", "Pv",
# "Ir Id" instead of "OTP", "KYC", "PV", "IR ID") -- corrected here rather
# than leaving the raw constant name on screen, which is what actually
# prompted this normalization (a bare "DISTRIBUTOR_LOGIN_METHOD" reads as
# code, not a setting a non-technical admin can recognize).
_LABEL_WORD_OVERRIDES = {
    "Otp": "OTP",
    "Kyc": "KYC",
    "Pv": "PV",
    "Ir": "IR",
    "Id": "ID",
    "Sms": "SMS",
    "Whatsapp": "WhatsApp",
    # str.title() capitalizes only the letter right after the digit --
    # "ADMIN_2FA_ENABLED".title() comes out "Admin 2Fa Enabled", not "2FA".
    "2Fa": "2FA",
}


def _humanize_setting_name(name):
    words = name.replace("_", " ").title().split(" ")
    return " ".join(_LABEL_WORD_OVERRIDES.get(word, word) for word in words)


def _style_constance_form_fields(form):
    """Constance builds its own field widgets per Python type (BooleanField/
    IntegerField/DecimalField/CharField, or the bounded custom fields in
    CONSTANCE_ADDITIONAL_FIELDS) with no Bancostore styling at all -- this
    applies the same Tailwind classes every other admin_portal form already
    uses (_INPUT_CLASS/_SELECT_CLASS) and the sr-only-peer checkbox pattern
    catalog_product_form.html established for is_active/is_featured,
    without touching constance's own field types, bounds, or validation.

    ADMIN_2FA_ENABLED is force-disabled here, not just styled -- mandatory
    2FA for admin accounts is a hard SPEC.md Boundary that already doesn't
    depend on this setting's value at enforcement time (this codebase's
    own test_2fa_requirement_cannot_be_bypassed_via_settings_toggle proves
    that), but a settings page that visibly lets an admin *think* they can
    turn it off is a real footgun in its own right. Django's `disabled`
    form fields ignore whatever the client actually POSTs and use the
    field's initial value instead, so this is real tamper-resistance, not
    just a greyed-out look."""
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, forms.HiddenInput):
            continue
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs["class"] = "sr-only peer"
        elif isinstance(widget, forms.Select):
            widget.attrs["class"] = _SELECT_CLASS
        elif isinstance(widget, forms.Textarea):
            widget.attrs["class"] = _INPUT_CLASS
            widget.attrs["rows"] = 6 if name in _LONG_TEXT_SETTINGS else 2
        else:
            widget.attrs["class"] = _INPUT_CLASS
        if name == "ADMIN_2FA_ENABLED":
            field.disabled = True


def _constance_field_context(name, options, form):
    """The one piece of constance.admin.ConstanceAdmin.get_config_value this
    page actually needs: which form field to render and how (checkbox,
    textarea, or a plain text/number/select input) -- not reimplemented,
    since that function doesn't use `self` and duplicating its shape here
    (rather than importing an admin-only method) keeps this view
    independent of Django Admin internals."""
    widget = form[name].field.widget
    return {
        "name": name,
        "label": _humanize_setting_name(name),
        "help_text": options[1],
        "form_field": form[name],
        "is_checkbox": isinstance(widget, forms.CheckboxInput),
        "is_textarea": isinstance(widget, forms.Textarea),
    }


@login_required(login_url="two_factor:login")
def platform_settings(request):
    """Task 28. All ~77 business-rule settings on one page, grouped into
    the same CONSTANCE_CONFIG_FIELDSETS categories admin already uses in
    Django Admin's constance change list, switched between via vertical
    tabs (apps/admin_portal/templates -- Alpine.js show/hide, not a page
    reload) rather than 10 separate pages. Every field stays present in the
    DOM at once regardless of which tab is visible: BancostoreConstanceForm
    validates and saves every setting together in one submission (its
    version-hash staleness check and cross-field WITHDRAWAL_AMOUNT
    validation both operate over the whole form), so there's no way to
    save just one group's fields even if the UI only shows one at a time."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    initial = get_values()
    if request.method == "POST":
        form = BancostoreConstanceForm(
            initial=initial, request=request, data=request.POST, files=request.FILES
        )
        # Must run before is_valid()/save(), not after -- ADMIN_2FA_ENABLED's
        # field.disabled=True (set inside this call) is what makes Django's
        # _clean_fields() read that field from the form's own initial value
        # instead of the submitted POST data. Calling this after save() (a
        # real bug caught via a live-browser round-trip test, not just
        # reasoning about it) left the field a normal, non-disabled
        # BooleanField at the moment of validation, so a real browser
        # submission -- which never includes a disabled checkbox's value at
        # all -- silently saved False, even though the rendered page showed
        # it locked on.
        _style_constance_form_fields(form)
        if form.is_valid():
            form.save()
            messages.success(request, "Platform settings updated successfully.")
            return redirect("admin_portal:platform_settings")
        messages.error(
            request,
            "Some settings couldn't be saved. Check the highlighted fields below.",
        )
    else:
        form = BancostoreConstanceForm(initial=initial, request=request)
        _style_constance_form_fields(form)

    groups = [
        {
            "title": title,
            "icon": _GROUP_ICONS[title],
            "fields": [
                _constance_field_context(name, CONSTANCE_CONFIG[name], form)
                for name in field_names
            ],
        }
        for title, field_names in CONSTANCE_CONFIG_FIELDSETS.items()
    ]

    context = {
        "form": form,
        "groups": groups,
        "active_nav": "platform_settings",
    }
    return render(request, "admin_portal/platform_settings.html", context)
