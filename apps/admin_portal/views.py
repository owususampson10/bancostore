from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render

from constance import config

from apps.distributors.models import Distributor
from apps.distributors.services import approve_kyc, reject_kyc
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    InsufficientWalletBalance,
    KycNotApproved,
    PayoutDestinationNotSet,
    WithdrawalRequestNotFound,
    WithdrawalRequestNotPending,
    approve_withdrawal_request,
    reject_withdrawal_request,
)

from .permissions import is_admin_portal_staff

# Task 23. Mirrors apps.withdrawal.admin's own _APPROVE_FAILURE_MESSAGES
# exactly -- same exceptions, same admin-facing text, just surfaced via
# Django's messages framework instead of the bulk-action results summary.
_APPROVE_FAILURE_MESSAGES = {
    KycNotApproved: "KYC is no longer approved.",
    PayoutDestinationNotSet: "Payout destination is no longer set.",
    InsufficientWalletBalance: "Wallet balance is now insufficient.",
    WithdrawalRequestNotFound: "This request no longer exists.",
    WithdrawalRequestNotPending: "This request has already been handled.",
}


@login_required(login_url="two_factor:login")
def dashboard(request):
    """Task 22 follow-up: the admin's post-login landing page. Without
    this, AdminLoginView.get_success_url() had nowhere branded to send a
    just-logged-in admin and fell back to Django Admin's raw /admin/
    index -- directly undermining this whole initiative (the admin should
    never see the unstyled system panel, even for a few seconds). Honest
    placeholder for now, matching apps.distributors.views.dashboard's own
    "you're logged in, here's the one real thing you can do today"
    pattern -- KYC Review is the only built admin_portal feature so far."""
    if not is_admin_portal_staff(request.user):
        raise PermissionDenied
    return render(request, "admin_portal/dashboard.html", {"active_nav": "dashboard"})


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
