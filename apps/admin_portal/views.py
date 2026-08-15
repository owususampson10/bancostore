from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Prefetch, ProtectedError, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from constance import config
from constance.utils import get_values

from apps.catalog.models import Category, Product, Review
from apps.catalog.services import normalize_primary_image
from apps.commissions.models import CommissionCycleRun
from apps.commissions.services import COMMISSION_TRANSACTION_TYPES
from apps.compliance.models import EscrowLedger
from apps.compliance.services import get_retail_distributor_ratio, get_unified_audit_log
from apps.distributors.models import Distributor
from apps.distributors.services import approve_kyc, reject_kyc
from apps.notifications.models import NotificationTemplate
from apps.notifications.template_registry import PLACEHOLDERS_BY_KEY
from apps.orders.models import Order, OrderItem
from apps.orders.services import (
    ADVANCEABLE_STATUSES,
    advance_order_status,
    cancel_or_refund_order,
    is_legal_order_status_transition,
)
from apps.pages.models import SocialMediaLink
from apps.pages.social_icons import SOCIAL_ICONS, detect_platform_from_url
from apps.platform_settings.admin import BancostoreConstanceForm
from apps.platform_settings.config import (
    CONSTANCE_CONFIG,
    CONSTANCE_CONFIG_FIELDSETS,
    humanize_identifier_name,
)
from apps.promotions.models import Banner, DiscountCode
from apps.reporting.models import ReportRollupRun
from apps.reporting.services import (
    bucket_revenue_series,
    get_best_selling_products_report,
    get_commissions_vs_revenue_report,
    get_new_vs_returning_customers_report,
    get_order_summary_report,
)
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
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)
from bancostore.exports import export_as_csv, export_as_pdf

from .forms import (
    _INPUT_CLASS,
    _SELECT_CLASS,
    MAX_PRODUCT_IMAGES,
    BannerForm,
    CategoryForm,
    DiscountCodeForm,
    NotificationTemplateForm,
    ProductForm,
    ProductVariantFormSet,
    SocialMediaLinkForm,
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

    # Task 47b. None (not 0) when there are no paid orders yet -- an
    # undefined ratio, distinct from a genuinely-0%-retail platform.
    retail_distributor_ratio = get_retail_distributor_ratio()
    retail_ratio_below_threshold = (
        retail_distributor_ratio is not None
        and retail_distributor_ratio < config.RETAIL_PV_MINIMUM_PERCENT
    )

    # Task 47c (Financial Overview, SPEC_PHASE2.md 12.6). All-time, not
    # "this week" like the Business Snapshot cards above -- these are
    # cumulative totals to date. Reuses the exact same "paid" exclusion
    # set as orders_this_week_value above.
    total_revenue = Order.objects.exclude(
        status__in=[Order.Status.PENDING, Order.Status.CANCELLED, Order.Status.REFUNDED]
    ).aggregate(total=Sum("total"))["total"] or Decimal("0")
    total_commissions_paid = WalletTransaction.objects.filter(
        transaction_type__in=COMMISSION_TRANSACTION_TYPES,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    # Only PAID withdrawals actually remitted tax to GRA -- a rejected or
    # payout-failed-reversed request never really withheld anything (Task
    # 16's reversal path credits the wallet back in full).
    total_withholding_tax_remitted = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.Status.PAID
    ).aggregate(total=Sum("tax_amount"))["total"] or Decimal("0")
    # A read-only fallback, not get_or_create -- this is a GET view and
    # must never write. Mirrors credit_escrow/reverse_escrow's own "the
    # pk=1 row may legitimately be absent" reasoning without the side
    # effect: a hard .get(pk=1) would 500 the whole dashboard the moment
    # that row is missing, instead of just showing GHS 0.00.
    escrow_balance = EscrowLedger.objects.filter(pk=1).values_list(
        "balance", flat=True
    ).first() or Decimal("0")

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
        "retail_distributor_ratio": retail_distributor_ratio,
        "retail_pv_minimum_percent": config.RETAIL_PV_MINIMUM_PERCENT,
        "retail_ratio_below_threshold": retail_ratio_below_threshold,
        "total_revenue": total_revenue,
        "total_commissions_paid": total_commissions_paid,
        "total_withholding_tax_remitted": total_withholding_tax_remitted,
        "escrow_balance": escrow_balance,
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
def withdrawal_review_export(request):
    """Task 45: bancostore.exports.export_as_csv's first real consumer --
    exports exactly the same SUBMITTED-only queryset withdrawal_review_queue
    itself shows (this codebase's established "export what the screen
    shows" convention, matching distributor_directory_export), including
    the tax figures Section 12.3's GRA withholding-tax review needs
    (amount/tax/net). Name-fallback logic (full_name, else the KYC-approved
    DiditVerification's extracted_full_name, else blank) mirrors the
    template's own `{% firstof %}` exactly -- same duplicated pattern
    already repeated 3x across this file's other withdrawal views, not a
    new one introduced here."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    withdrawal_requests = (
        WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.SUBMITTED)
        .select_related("distributor", "distributor__didit_verification")
        .order_by("created_at")
    )

    def _rows():
        for withdrawal_request in withdrawal_requests:
            distributor = withdrawal_request.distributor
            verification = getattr(distributor, "didit_verification", None)
            display_name = distributor.full_name or (
                verification.extracted_full_name if verification else ""
            )
            yield [
                display_name,
                distributor.ir_id or "",
                withdrawal_request.amount,
                withdrawal_request.tax_amount,
                withdrawal_request.net_amount,
                withdrawal_request.created_at.strftime("%Y-%m-%d"),
            ]

    return export_as_csv(
        "withdrawal-requests.csv",
        [
            "Distributor",
            "IR ID",
            "Amount (GHS)",
            "Tax (GHS)",
            "Net Payout (GHS)",
            "Date Submitted",
        ],
        _rows(),
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


@login_required(login_url="two_factor:login")
def distributor_directory_export(request):
    """Task 23 follow-up. Exports exactly what the current search/filter
    shows (via the same _filtered_distributors as the page itself), not
    the whole table unconditionally -- an admin who searched down to one
    distributor and clicks Export should get that one row, not all 1,248.

    Task 45: rebuilt on the shared bancostore.exports.export_as_csv
    utility -- this view's own formula-injection guard (`_csv_safe`,
    Task 23 follow-up) became that utility's `csv_safe_cell`, applied
    unconditionally to every cell rather than by this view remembering
    which specific columns are "risky" free text. Behavior unchanged
    (verified: this view's own pre-existing test suite passes
    unmodified), not a new feature."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    distributors, _query, _status = _filtered_distributors(request)

    rows = (
        [
            distributor.full_name,
            distributor.ir_id or "",
            distributor.phone_number,
            distributor.rank,
            distributor.get_kyc_status_display(),
            "Active" if distributor.user.is_active else "Suspended",
        ]
        for distributor in distributors
    )
    return export_as_csv(
        "distributors.csv",
        ["Full Name", "IR ID", "Phone Number", "Rank", "KYC Status", "Account Status"],
        rows,
    )


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


@login_required(login_url="two_factor:login")
def audit_log(request):
    """Task 47e. Read-only, cross-model observability -- presentation
    only, no service-layer changes, matching commission_oversight's own
    established shape above (Paginator(20), no htmx real-time filtering,
    date-range only -- the acceptance criteria names no other filter).
    get_unified_audit_log (apps.compliance.services) does the real work:
    merging every HistoricalRecords()-tracked model's history with
    PlatformSettingChange into one timestamp-sorted list, already bounded
    by the requested date range before this view ever sees it.

    Paginator works identically on the returned plain list as it does on
    a queryset elsewhere in this codebase -- no special-casing needed."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    date_from = parse_date(request.GET.get("date_from", "").strip())
    date_to = parse_date(request.GET.get("date_to", "").strip())

    entries = get_unified_audit_log(date_from=date_from, date_to=date_to)
    paginator = Paginator(entries, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    # Task 48: a stable per-row id for the click-to-open detail modal's
    # json_script lookup -- only needs to be unique within this one page
    # (not globally), so the page's own row index is enough.
    for index, entry in enumerate(page_obj.object_list):
        entry["row_id"] = f"audit-entry-{index}"

    return render(
        request,
        "admin_portal/audit_log.html",
        {
            "page_obj": page_obj,
            "date_from": date_from,
            "date_to": date_to,
            "active_nav": "audit_log",
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
# Sales & Revenue Reporting (Task 46b, ADR-0010)
# ---------------------------------------------------------------------------

_REPORT_DEFAULT_RANGE_DAYS = 30
_REPORT_GRANULARITIES = ("day", "week", "month")


def _sales_revenue_report_params(request):
    """Shared by the report page and both export views -- mirrors
    _filtered_orders' own "one function, every consumer reads the same
    filtered set" shape, so an export always matches exactly what the
    screen currently shows. Defaults to the trailing 30 days (today
    inclusive) when no explicit range is given, a reasonable first-visit
    window rather than an unbounded "since the dawn of time" default.

    Task 46c: also computes best-selling-products, new-vs-returning-
    customers, and commissions-vs-revenue for the same date range --
    one shared params function for the whole report page (46b's revenue/
    status/zone sections plus 46c's three), not a second near-duplicate
    helper."""
    today = timezone.now().date()
    date_from = parse_date(request.GET.get("date_from", "").strip()) or (
        today - timedelta(days=_REPORT_DEFAULT_RANGE_DAYS - 1)
    )
    date_to = parse_date(request.GET.get("date_to", "").strip()) or today

    granularity = request.GET.get("granularity", "day").strip()
    if granularity not in _REPORT_GRANULARITIES:
        granularity = "day"

    report = get_order_summary_report(date_from, date_to)
    revenue_series = bucket_revenue_series(
        report["daily_series"], granularity=granularity
    )
    best_selling_products = get_best_selling_products_report(date_from, date_to)
    customer_report = get_new_vs_returning_customers_report(date_from, date_to)
    commissions_report = get_commissions_vs_revenue_report(date_from, date_to)

    return (
        date_from,
        date_to,
        granularity,
        report,
        revenue_series,
        best_selling_products,
        customer_report,
        commissions_report,
    )


@login_required(login_url="two_factor:login")
def sales_revenue_report(request):
    """Task 46b (ADR-0010). Reads exclusively from DailyOrderRollup via
    get_order_summary_report -- never a live Order-table scan, per
    ADR-0010's own decision. A date with no rollup row yet (today, or a
    day the nightly job hasn't reached) simply shows no data for that
    day; there is no live fallback."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    (
        date_from,
        date_to,
        granularity,
        report,
        revenue_series,
        best_selling_products,
        customer_report,
        commissions_report,
    ) = _sales_revenue_report_params(request)
    last_run = (
        ReportRollupRun.objects.filter(succeeded=True).order_by("-run_at").first()
    )

    context = {
        "date_from": date_from,
        "date_to": date_to,
        "granularity": granularity,
        "granularity_choices": [
            ("day", "Day"),
            ("week", "Week"),
            ("month", "Month"),
        ],
        "report": report,
        "revenue_series": revenue_series,
        "order_total": sum(report["order_status_counts"].values()),
        "delivery_fees_total": sum(report["delivery_fees_by_zone"].values()),
        "best_selling_products": best_selling_products,
        "customer_report": customer_report,
        "commissions_report": commissions_report,
        "last_rollup_run_at": last_run.run_at if last_run else None,
        "active_nav": "reports",
    }

    if request.htmx:
        return render(
            request, "admin_portal/partials/sales_revenue_report_results.html", context
        )

    return render(request, "admin_portal/sales_revenue_report.html", context)


@login_required(login_url="two_factor:login")
def sales_revenue_report_export_csv(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    (
        date_from,
        date_to,
        granularity,
        report,
        revenue_series,
        best_selling_products,
        customer_report,
        commissions_report,
    ) = _sales_revenue_report_params(request)

    rows = [
        ["Revenue", report["revenue"]],
        [],
        [f"Revenue by {granularity}"],
        ["Period", "Revenue (GHS)"],
        *[[row["period"], row["revenue"]] for row in revenue_series],
        [],
        ["Orders by Status"],
        *[
            [label, report["order_status_counts"][key]]
            for key, label in [
                ("pending", "Pending"),
                ("confirmed", "Confirmed"),
                ("processing", "Processing"),
                ("dispatched", "Dispatched"),
                ("delivered", "Delivered"),
                ("cancelled", "Cancelled"),
                ("refunded", "Refunded"),
            ]
        ],
        [],
        ["Delivery Fees by Zone (GHS)"],
        ["Kumasi", report["delivery_fees_by_zone"]["kumasi"]],
        ["Accra", report["delivery_fees_by_zone"]["accra"]],
        ["Other Regions", report["delivery_fees_by_zone"]["other_regions"]],
        [],
        ["Best-Selling Products"],
        ["Product", "Units Sold", "Revenue (GHS)"],
        *[
            [row["product_name"], row["units_sold"], row["revenue"]]
            for row in best_selling_products
        ],
        [],
        ["New vs Returning Customers"],
        ["New Customers", customer_report["new_customers"]],
        ["Returning Customers", customer_report["returning_customers"]],
        [],
        ["Commissions vs Revenue"],
        ["Revenue (GHS)", commissions_report["revenue"]],
        ["Commissions Paid (GHS)", commissions_report["commissions_paid"]],
    ]
    return export_as_csv(
        f"sales-revenue-report-{date_from}-to-{date_to}.csv",
        [f"Sales & Revenue Report: {date_from} to {date_to}"],
        rows,
    )


@login_required(login_url="two_factor:login")
def sales_revenue_report_export_pdf(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    (
        date_from,
        date_to,
        granularity,
        report,
        revenue_series,
        best_selling_products,
        customer_report,
        commissions_report,
    ) = _sales_revenue_report_params(request)
    return export_as_pdf(
        f"sales-revenue-report-{date_from}-to-{date_to}.pdf",
        "admin_portal/sales_revenue_report_pdf.html",
        {
            "date_from": date_from,
            "date_to": date_to,
            "granularity": granularity,
            "report": report,
            "revenue_series": revenue_series,
            "best_selling_products": best_selling_products,
            "customer_report": customer_report,
            "commissions_report": commissions_report,
        },
    )


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
# Promotional Banners (Task 42)
# ---------------------------------------------------------------------------


@login_required(login_url="two_factor:login")
def catalog_banner_list(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    banners = Banner.objects.select_related("product", "category")
    active_ids = set(Banner.objects.active().values_list("pk", flat=True))
    return render(
        request,
        "admin_portal/catalog_banner_list.html",
        {
            "banners": banners,
            "active_ids": active_ids,
            "today": timezone.localdate(),
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def catalog_banner_create(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    if request.method == "POST":
        form = BannerForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Banner created.")
            return redirect("admin_portal:catalog_banner_list")
    else:
        form = BannerForm()

    return render(
        request,
        "admin_portal/catalog_banner_form.html",
        {
            "form": form,
            "is_edit": False,
            "products": Product.objects.order_by("name"),
            "categories": Category.objects.order_by("name"),
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def catalog_banner_edit(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    banner = get_object_or_404(Banner, pk=pk)
    if request.method == "POST":
        form = BannerForm(request.POST, request.FILES, instance=banner)
        if form.is_valid():
            form.save()
            messages.success(request, "Banner updated.")
            return redirect("admin_portal:catalog_banner_list")
    else:
        form = BannerForm(instance=banner)

    return render(
        request,
        "admin_portal/catalog_banner_form.html",
        {
            "form": form,
            "banner": banner,
            "is_edit": True,
            "products": Product.objects.order_by("name"),
            "categories": Category.objects.order_by("name"),
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def catalog_banner_delete(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    banner = get_object_or_404(Banner, pk=pk)
    if request.method == "POST":
        banner.delete()
        messages.success(request, "Banner deleted.")
    return redirect("admin_portal:catalog_banner_list")


# ---------------------------------------------------------------------------
# Discount Codes (Task 43a)
# ---------------------------------------------------------------------------


@login_required(login_url="two_factor:login")
def discount_code_list(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    codes = DiscountCode.objects.all()
    return render(
        request,
        "admin_portal/discount_code_list.html",
        {
            "codes": codes,
            "today": timezone.localdate(),
            "active_nav": "catalog",
        },
    )


@login_required(login_url="two_factor:login")
def discount_code_create(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    if request.method == "POST":
        form = DiscountCodeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Discount code created.")
            return redirect("admin_portal:discount_code_list")
    else:
        form = DiscountCodeForm()

    return render(
        request,
        "admin_portal/discount_code_form.html",
        {"form": form, "is_edit": False, "active_nav": "catalog"},
    )


@login_required(login_url="two_factor:login")
def discount_code_edit(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    code = get_object_or_404(DiscountCode, pk=pk)
    if request.method == "POST":
        form = DiscountCodeForm(request.POST, instance=code)
        if form.is_valid():
            form.save()
            messages.success(request, "Discount code updated.")
            return redirect("admin_portal:discount_code_list")
    else:
        form = DiscountCodeForm(instance=code)

    return render(
        request,
        "admin_portal/discount_code_form.html",
        {"form": form, "code": code, "is_edit": True, "active_nav": "catalog"},
    )


@login_required(login_url="two_factor:login")
def discount_code_delete(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    code = get_object_or_404(DiscountCode, pk=pk)
    if request.method == "POST":
        code.delete()
        messages.success(request, "Discount code deleted.")
    return redirect("admin_portal:discount_code_list")


# ---------------------------------------------------------------------------
# Notification Templates (Task 48a)
# ---------------------------------------------------------------------------


@login_required(login_url="two_factor:login")
def notification_template_list(request):
    """List + edit only -- no create/delete. Every NotificationTemplate.Key
    is pre-seeded by migration (0006_seed_notification_templates), so
    there's no freeform "new key" for an admin to create, and deleting a
    row would leave a real send site's render_or_default() lookup
    falling back to its hardcoded default rather than the admin's own
    edited wording -- a silent regression, not a safe no-op, so this
    screen doesn't offer that action at all."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    templates = NotificationTemplate.objects.all()
    return render(
        request,
        "admin_portal/notification_template_list.html",
        {"templates": templates, "active_nav": "notifications"},
    )


@login_required(login_url="two_factor:login")
def notification_template_edit(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    template = get_object_or_404(NotificationTemplate, pk=pk)
    if request.method == "POST":
        form = NotificationTemplateForm(request.POST, instance=template)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification template updated.")
            return redirect("admin_portal:notification_template_list")
    else:
        form = NotificationTemplateForm(instance=template)

    return render(
        request,
        "admin_portal/notification_template_form.html",
        {
            "form": form,
            "template": template,
            "placeholders": PLACEHOLDERS_BY_KEY.get(template.key, []),
            "active_nav": "notifications",
        },
    )


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
# Product Reviews (Task 41b) -- a global moderation queue, matching the
# KYC/withdrawal/order queues' own "one queue across everything, not
# per-product" convention, rather than a review list embedded on each
# product's own edit page.
# ---------------------------------------------------------------------------


@login_required(login_url="two_factor:login")
def review_moderation_list(request):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    reviews = (
        Review.objects.select_related("user", "product")
        # Pending first -- that's the actual queue an admin needs to work
        # through; already-approved reviews are shown for context/undo,
        # not as the primary thing to act on. "-pk" tiebreaker (code-review-
        # and-quality finding): matches order_history/team/earnings_history's
        # own established fix for the same pagination-stability gap a bare
        # created_at ordering has on a timestamp collision.
        .order_by("is_approved", "-created_at", "-pk")
    )
    paginator = Paginator(reviews, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin_portal/review_moderation.html",
        {"page_obj": page_obj, "active_nav": "catalog"},
    )


@login_required(login_url="two_factor:login")
@require_POST
def review_approve(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    review = get_object_or_404(Review, pk=pk)
    review.is_approved = True
    review.save(update_fields=["is_approved"])
    messages.success(request, "Review approved.")
    return redirect("admin_portal:review_moderation_list")


@login_required(login_url="two_factor:login")
@require_POST
def review_delete(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    review = get_object_or_404(Review, pk=pk)
    review.delete()
    messages.success(request, "Review deleted.")
    return redirect("admin_portal:review_moderation_list")


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
    "Product & Inventory Settings": "inventory_2",
    "Promotions Settings": "sell",
    "Reporting Settings": "monitoring",
    "Compliance Settings": "gavel",
    "KYC Settings": "fact_check",
    "IR ID Number Settings": "badge",
    "Payment Gateway Settings": "point_of_sale",
    "General Platform Settings": "settings_suggest",
    "Notification & Communication Settings": "forum",
}


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
        if name in _LOCKED_CURRENCY_SETTINGS:
            field.disabled = True


# Task 36h: locked to GHS, not a themed dropdown -- grep-confirmed neither
# setting is read anywhere in the app yet (every price display and the
# Paystack API calls hardcode GHS), and the Paystack merchant account
# itself is GHS-only, so a working-looking currency picker would be a real
# footgun: an admin could pick USD expecting prices/payments to switch,
# and nothing would happen. User-confirmed decision (asked directly,
# multi-currency support is real, separate, unrequested scope) to lock
# this to GHS rather than offer a dropdown that silently does nothing.
# CONSTANCE_ADDITIONAL_FIELDS["currency_field"]'s choices are restricted
# to a single GHS entry (apps/platform_settings/config.py); field.disabled
# below is defense-in-depth on top of that, matching ADMIN_2FA_ENABLED's
# own established pattern in this same function.
_LOCKED_CURRENCY_SETTINGS = {"CURRENCY", "CURRENCY_SYMBOL"}


def _constance_field_context(name, options, form):
    """The one piece of constance.admin.ConstanceAdmin.get_config_value this
    page actually needs: which form field to render and how (checkbox,
    textarea, or a plain text/number/select input) -- not reimplemented,
    since that function doesn't use `self` and duplicating its shape here
    (rather than importing an admin-only method) keeps this view
    independent of Django Admin internals."""
    bound_field = form[name]
    widget = bound_field.field.widget
    is_locked_currency = name in _LOCKED_CURRENCY_SETTINGS
    context = {
        "name": name,
        "label": humanize_identifier_name(name),
        "help_text": options[1],
        "form_field": bound_field,
        "is_checkbox": isinstance(widget, forms.CheckboxInput),
        "is_textarea": isinstance(widget, forms.Textarea),
        "is_locked_currency": is_locked_currency,
    }
    if is_locked_currency:
        # bound_field.value() on a disabled field always resolves to the
        # real stored initial value (Django's Field.bound_data short-
        # circuits to `initial` for disabled fields, even on a POST re-
        # render) -- never whatever a crafted request might have submitted.
        # CodeRabbit finding: a bare dict lookup crashes the *entire*
        # Platform Settings page (all ~76 settings, not just this field)
        # with an uncaught KeyError if a legacy/stale value ever ends up
        # stored (e.g. a direct Redis write, a rollback) that predates
        # this field being locked to a single GHS choice -- .get() with
        # the raw stored value as its own fallback shows something honest
        # instead of crashing.
        stored_value = bound_field.value()
        context["locked_label"] = dict(bound_field.field.choices).get(
            stored_value, stored_value
        )
    return context


def _build_constance_groups(form):
    return [
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


# Task 36c: Social Links moved from its own sidebar item into this page's
# General Platform Settings tab, per explicit user request. Looked up by
# title rather than hardcoded as a numeric index so reordering
# CONSTANCE_CONFIG_FIELDSETS can never silently point this at the wrong tab.
_GENERAL_SETTINGS_GROUP_TITLE = "General Platform Settings"


def _general_settings_tab_index():
    return list(CONSTANCE_CONFIG_FIELDSETS.keys()).index(_GENERAL_SETTINGS_GROUP_TITLE)


def _resolve_active_group(request, default=None):
    """?tab=<index> selects which vertical-tab panel is open on page load
    -- used by the Social Links create/update redirects so saving a link
    lands back on the General tab it was edited from, not tab 0."""
    raw = request.GET.get("tab")
    total = len(CONSTANCE_CONFIG_FIELDSETS)
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None and 0 <= value < total:
            return value
    return default if default is not None else 0


def _platform_settings_context(request, *, social_link_kwargs=None):
    """Shared by platform_settings (GET/POST) and the Social Links create/
    update views' failure paths (which must re-render this exact page,
    not a separate one, since Social Links now lives inside it)."""
    initial = get_values()
    form = BancostoreConstanceForm(initial=initial, request=request)
    _style_constance_form_fields(form)
    context = {
        "form": form,
        "groups": _build_constance_groups(form),
        "active_nav": "platform_settings",
        "general_settings_tab_index": _general_settings_tab_index(),
    }
    context.update(
        _social_links_settings_context(request, **(social_link_kwargs or {}))
    )
    return context


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
    save just one group's fields even if the UI only shows one at a time.

    Task 36c: also renders the Social Links management UI (its own,
    separate <form> elements -- see _social_links_settings_context) inside
    this page's General Platform Settings tab."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    if request.method == "POST":
        initial = get_values()
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
        context = {
            "form": form,
            "groups": _build_constance_groups(form),
            "active_nav": "platform_settings",
            "general_settings_tab_index": _general_settings_tab_index(),
            # Code-review finding: this branch never set active_group, so
            # a validation failure (e.g. the cross-field WITHDRAWAL_AMOUNT
            # check) always bounced the admin back to tab 0 -- even if the
            # page was loaded via ?tab=N (the same-URL POST preserves the
            # query string), matching the GET path below.
            "active_group": _resolve_active_group(request),
        }
        context.update(_social_links_settings_context(request))
        return render(request, "admin_portal/platform_settings.html", context)

    context = _platform_settings_context(request)
    context["active_group"] = _resolve_active_group(request)
    return render(request, "admin_portal/platform_settings.html", context)


# ---------------------------------------------------------------------------
# Task 35: Social Media Links -- admin-managed, unlimited-count footer links
# ---------------------------------------------------------------------------


def _social_links_settings_context(request, add_form=None, row_forms_override=None):
    """Shared by platform_settings/social_link_create/social_link_update so
    a failed create/update re-renders the exact same page (with that one
    form's errors visible) instead of a separate error page -- matching
    the "redirect on success, re-render with errors on failure" shape every
    other admin_portal create/edit view already uses, just applied to one
    shared settings page instead of a dedicated form page since rows edit
    in place."""
    links = list(SocialMediaLink.objects.all())
    row_forms_override = row_forms_override or {}
    rows = []
    for link in links:
        form = row_forms_override.get(link.pk) or SocialMediaLinkForm(instance=link)
        rows.append(
            {
                "link": link,
                "form": form,
                "element_id": f"social-link-initial-{link.pk}",
                # CodeRabbit finding: raw values were interpolated straight
                # into Alpine's x-data JS string (guarded by |escapejs, but
                # this project's own established convention after a real
                # incident on the checkout page -- Task 17 -- is
                # json_script + a script-tag read, never inline
                # interpolation at all, escaped or not). form.*.value
                # reflects the just-submitted (possibly invalid) value on
                # a failed save, not just the saved link, so this must
                # read from the form, not the link, to show what the
                # admin actually typed.
                "initial": {
                    "platform": form["platform"].value() or link.platform,
                    "icon_color": form["icon_color"].value() or link.icon_color,
                },
            }
        )

    add_form = add_form or SocialMediaLinkForm()
    return {
        "rows": rows,
        "add_form": add_form,
        "add_form_element_id": "social-link-add-form-initial",
        "add_form_initial": {
            "platform": add_form["platform"].value() or "custom",
            "icon_color": add_form["icon_color"].value() or "#FFFFFF",
        },
        "at_max": len(links) >= config.MAX_SOCIAL_MEDIA_LINKS,
        "max_links": config.MAX_SOCIAL_MEDIA_LINKS,
        # Rendered server-side (real SVG/material-icon markup for every
        # curated platform), toggled client-side only via Alpine x-show --
        # never built up as an HTML string in JS, so there is no dynamic
        # markup-construction code path to review for injection risk at all.
        "all_icons": list(SOCIAL_ICONS.values()),
    }


def _save_new_social_link_with_next_order(form):
    """CodeRabbit finding: a bare `.aggregate(Max("order")) + 1` read-then-
    write can assign duplicate order values under two truly concurrent
    creates. Locks the whole table for the duration of the read+write
    (this table stays small -- at most MAX_SOCIAL_MEDIA_LINKS rows -- so a
    full-table lock is cheap here, unlike a per-row lock pattern) using
    this codebase's own established concurrency helper
    (bancostore/concurrency.py, the same one apps.catalog.services
    .decrement_stock uses) instead of reinventing locking."""

    def _attempt():
        with transaction.atomic():
            locked_links = select_for_update_nowait_if_supported(
                SocialMediaLink.objects.all()
            )
            current_max = locked_links.aggregate(m=Max("order"))["m"] or 0
            link = form.save(commit=False)
            link.order = current_max + 1
            link.save()
            return link

    return retry_on_lock_contention(_attempt)


@login_required(login_url="two_factor:login")
def social_link_create(request):
    """POST-only. Every write goes through SocialMediaLinkForm's is_valid()
    -- never a bare .save() from raw request.POST -- so url/icon_color's
    validators (and the MAX_SOCIAL_MEDIA_LINKS cap in the form's clean())
    are guaranteed to actually run (doubt-driven-development finding)."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied
    if request.method != "POST":
        raise PermissionDenied

    form = SocialMediaLinkForm(request.POST)
    if form.is_valid():
        link = _save_new_social_link_with_next_order(form)
        messages.success(request, f'"{link.name}" added.')
        return redirect(
            f"{reverse('admin_portal:platform_settings')}"
            f"?tab={_general_settings_tab_index()}"
        )

    messages.error(request, "Couldn't add that link. Check the highlighted fields.")
    context = _platform_settings_context(request, social_link_kwargs={"add_form": form})
    context["add_form_open"] = True
    context["active_group"] = _general_settings_tab_index()
    return render(request, "admin_portal/platform_settings.html", context, status=400)


@login_required(login_url="two_factor:login")
def social_link_update(request, pk):
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied
    if request.method != "POST":
        raise PermissionDenied

    link = get_object_or_404(SocialMediaLink, pk=pk)
    form = SocialMediaLinkForm(request.POST, instance=link)
    if form.is_valid():
        form.save()
        messages.success(request, f'"{link.name}" updated.')
        return redirect(
            f"{reverse('admin_portal:platform_settings')}"
            f"?tab={_general_settings_tab_index()}"
        )

    messages.error(request, "Couldn't save that link. Check the highlighted fields.")
    context = _platform_settings_context(
        request, social_link_kwargs={"row_forms_override": {link.pk: form}}
    )
    context["open_link_id"] = link.pk
    context["active_group"] = _general_settings_tab_index()
    return render(request, "admin_portal/platform_settings.html", context, status=400)


@login_required(login_url="two_factor:login")
def social_link_delete(request, pk):
    """POST-only, matches catalog_category_delete's shape exactly (shared
    _delete_confirm_modal.html partial, plain redirect back to the list)."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    link = get_object_or_404(SocialMediaLink, pk=pk)
    if request.method == "POST":
        name = link.name
        link.delete()
        messages.success(request, f'"{name}" deleted.')
    return redirect(
        f"{reverse('admin_portal:platform_settings')}"
        f"?tab={_general_settings_tab_index()}"
    )


@login_required(login_url="two_factor:login")
def social_link_detect_platform(request):
    """GET, called by the add/edit form's own JS on the URL field's blur
    event (fetch(), not htmx -- a plain JSON response is simpler and more
    responsive than swapping an HTML partial into a live Alpine scope for
    a single reactive value). Stateless -- no DB write, just parses the
    candidate URL and returns the detected platform slug; the admin can
    still pick a different one from the picker before saving, and the
    eventual save is validated independently regardless of what this
    returned. Admin-gated like every sibling endpoint here, not left open
    (doubt-driven-development finding: no legitimate reason for this to be
    public, and leaving it open would have widened the blast radius of any
    future bug in this view). The candidate URL is used only to compute a
    platform slug -- it is never echoed back into the response, so there
    is no reflected-content/scheme-injection surface here even though the
    input itself hasn't been validated as a real URL yet."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied

    candidate_url = request.GET.get("url", "")
    detected_platform = detect_platform_from_url(candidate_url)
    return JsonResponse({"platform": detected_platform})
