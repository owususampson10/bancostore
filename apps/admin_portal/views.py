import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

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
