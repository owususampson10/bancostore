import json
import logging
import math
from datetime import timedelta
from decimal import Decimal
from functools import wraps
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Max, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from constance import config
from django_ratelimit.decorators import ratelimit

from apps.accounts.permissions import is_distributor
from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import get_downline_tree
from apps.distributors.cooling_off_services import (
    CoolingOffPeriodExpired,
    NoRefundableStarterPackPurchase,
    calculate_cooling_off_refund,
    cancel_membership_and_refund,
)
from apps.notifications.otp import generate_otp, verify_otp
from apps.orders.services import confirm_order_payment
from apps.pv_ledger.models import MonthlyPersonalPv
from apps.wallet.models import WalletTransaction
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    AboveMaximumAmount,
    BelowMinimumAmount,
    InsufficientWalletBalance,
    KycNotApproved,
    PayoutDestinationNotSet,
    WithdrawalFrequencyMisconfigured,
    WithdrawalWindowActive,
    WithholdingTaxMisconfigured,
    submit_withdrawal_request,
)

# Task 16f PR 3: paystack_webhook is this project's one shared dispatcher
# for every Paystack event type (established by charge.success's own
# reg-/pack- prefix dispatch) -- withdrawal-specific dispatch logic stays
# a single .delay() call here, not duplicated withdrawal business logic.
from apps.withdrawal.tasks import TRANSFER_WEBHOOK_EVENTS, process_transfer_webhook_task

from .didit import DiditError
from .didit import create_verification_session as create_didit_session
from .didit import verify_webhook_signature as verify_didit_webhook_signature
from .forms import (
    DistributorForgotPasswordForm,
    DistributorLoginForm,
    DistributorRegistrationForm,
    DistributorSetNewPasswordForm,
    OTPVerificationForm,
    PayoutSettingsForm,
    WithdrawalRequestForm,
)
from .models import DiditVerification, Distributor, PendingRegistration
from .paystack import PaystackError, initialize_transaction, verify_webhook_signature
from .services import (
    MembershipCancelled,
    PendingRegistrationAlreadyConsumed,
    PendingRegistrationNotFound,
    StarterPackAlreadyConfirmed,
    attempt_distributor_login,
    consume_didit_result,
    consume_paid_registration,
    consume_paid_starter_pack,
    snapshot_payment_reference,
    snapshot_starter_pack_choice,
)
from .tasks import consume_didit_result_task

User = get_user_model()
logger = logging.getLogger(__name__)

AUTH_BACKEND = "apps.distributors.backends.PhoneNumberBackend"


def _redirect_if_cooling_off_cancelled(view_func):
    """Task 19 follow-up (CodeRabbit finding, user-confirmed fix):
    apps.distributors.backends.PhoneNumberBackend now lets a cooling-off-
    cancelled distributor log in specifically so they can claim the
    refund already credited to their own wallet -- this decorator is
    what actually restricts what they see once logged in. Applied only
    to the views that make no sense post-cancellation (no rank, no
    starter pack, nothing left to earn); withdrawal_request/
    withdrawal_history/payout_settings deliberately do NOT get this
    decorator, since those are exactly the views that let the refund
    actually be claimed. cancel_membership also doesn't need it -- its
    own GET already renders a correct, safe "already cancelled" ineligible
    state for this same distributor.

    A single shared decorator, not a repeated inline check in each view,
    so a future new distributor view can't silently reopen full access
    just by forgetting to add the check -- the risk CLAUDE.md's own Task
    15 "Deferred, not silently skipped" note already flagged for the
    unguarded request.user.distributor pattern, applied here to a case
    where forgetting it would be a real access-control regression, not
    just an inconsistency."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        distributor = getattr(request.user, "distributor", None)
        if distributor is not None and distributor.cooling_off_cancelled_at is not None:
            return redirect("distributors:withdrawal_request")
        return view_func(request, *args, **kwargs)

    return wrapper


@ratelimit(key="ip", rate="5/h", method="POST")
def register(request):
    """Task 10a: creates a PendingRegistration, not a live account -- the
    real User/Distributor is only created once the registration fee is
    confirmed paid (Task 10b, not built yet). See tests/feature/
    distributors/test_registration_pending.py and the doubt-driven-development
    design note in apps/distributors/models.py::PendingRegistration."""
    if request.method == "POST":
        form = DistributorRegistrationForm(request.POST)
        if form.is_valid():
            pending = PendingRegistration.objects.create(
                full_name=form.cleaned_data["full_name"],
                phone_number=str(form.cleaned_data["phone_number"]),
                email=form.cleaned_data.get("email", ""),
                address=form.cleaned_data["address"],
                area=form.cleaned_data["area"],
                landmark=form.cleaned_data.get("landmark", ""),
                password_hash=make_password(form.cleaned_data["password1"]),
                sponsor=form.cleaned_data["sponsor"],
            )
            request.session["pending_registration_token"] = str(pending.token)
            return redirect("distributors:pay_registration_fee")
    else:
        form = DistributorRegistrationForm(
            initial={"sponsor_ir_id": request.GET.get("ref", "")}
        )
    return render(request, "distributors/register.html", {"form": form})


@ratelimit(key="ip", rate="20/h", method="GET")
def pay_registration_fee(request):
    """Task 10b: initializes a Paystack transaction for the registration
    fee and redirects to the hosted checkout. See
    project_paystack_registration_payment_design (doubt-driven-development,
    2026-07-13) for why the fee and Paystack reference are snapshotted onto
    PendingRegistration here rather than re-derived later."""
    token = request.session.get("pending_registration_token")
    if not token:
        return redirect("distributors:register")

    try:
        pending = snapshot_payment_reference(token)
    except PendingRegistrationNotFound:
        return redirect("distributors:register")
    except PendingRegistrationAlreadyConsumed:
        return redirect("distributors:login")

    callback_url = request.build_absolute_uri(
        reverse("distributors:registration_payment_callback")
    )
    # Paystack requires an email on every transaction; the form's email
    # field is optional (SPEC.md Section 4 lists it as "for notifications
    # only"), so fall back to a synthetic address that satisfies the API
    # without claiming it's a real contact channel.
    email = pending.email or f"{pending.phone_number}@bancostore.test"

    try:
        data = initialize_transaction(
            email=email,
            amount_pesewas=pending.fee_amount_pesewas,
            reference=pending.payment_reference,
            callback_url=callback_url,
        )
    except PaystackError:
        logger.exception(
            "pay_registration_fee: Paystack initialize_transaction failed "
            "for token=%s",
            token,
        )
        return render(
            request, "distributors/pay_registration_fee.html", {"error": True}
        )

    return redirect(data["authorization_url"])


def registration_payment_callback(request):
    """The user's browser lands here after attempting payment on Paystack's
    hosted checkout. NOT the source of truth for account creation (the
    webhook is) -- but calls the same idempotent consume function as a
    fast-path, since Paystack's own redirect already carries the reference
    as a query param. See project_paystack_registration_payment_design.

    Auto-logs the user in once their account is confirmed created --
    but only if this request's own session is the one that started this
    specific registration (request.session["pending_registration_token"]
    matches). Section 14's flow expects a seamless continue-to-checkout
    experience with no separate login step, but logging in based on the
    `reference` query param alone would be exploitable: it can leak via
    browser history or a Referer header, and anyone holding a leaked
    reference would otherwise be logged into the real owner's account.
    Tying it to the session instead means only the browser that actually
    submitted the registration form gets the automatic login."""
    reference = request.GET.get("reference", "")
    if reference:
        consume_paid_registration(reference)

    token = request.session.get("pending_registration_token")
    if token:
        try:
            pending = PendingRegistration.objects.get(token=token)
        except PendingRegistration.DoesNotExist:
            pending = None

        if pending and pending.consumed_at is not None:
            try:
                distributor = Distributor.objects.select_related("user").get(
                    phone_number=pending.phone_number
                )
            except Distributor.DoesNotExist:
                distributor = None
            if distributor:
                auth_login(request, distributor.user, backend=AUTH_BACKEND)
                del request.session["pending_registration_token"]
                return redirect("distributors:select_starter_pack")

    return render(request, "distributors/registration_payment_callback.html")


@login_required(login_url="distributors:login")
@_redirect_if_cooling_off_cancelled
@ratelimit(key="user", rate="20/h", method="POST")
def select_starter_pack(request):
    """Task 10c: distributor chooses Starter Pack A or B; price/PV/rank
    read from django-constance (never hardcoded), Paystack charge, and on
    confirmed payment (consume_paid_starter_pack) rank is set from the
    pinned snapshot. Per Section 14 step 5, this purchase is what
    "officially activates" the distributor."""
    if request.method == "POST":
        choice = request.POST.get("pack")
        if choice not in ("A", "B"):
            return render(
                request,
                "distributors/select_starter_pack.html",
                {"error": "Please choose a valid starter pack."},
            )

        try:
            distributor = snapshot_starter_pack_choice(
                request.user.distributor.pk, choice
            )
        except (StarterPackAlreadyConfirmed, MembershipCancelled):
            return redirect("distributors:dashboard")

        callback_url = request.build_absolute_uri(
            reverse("distributors:starter_pack_payment_callback")
        )
        email = distributor.user.email or (
            f"{distributor.phone_number}@bancostore.test"
        )

        try:
            data = initialize_transaction(
                email=email,
                amount_pesewas=distributor.starter_pack_price_pesewas,
                reference=distributor.starter_pack_payment_reference,
                callback_url=callback_url,
            )
        except PaystackError:
            logger.exception(
                "select_starter_pack: Paystack initialize_transaction "
                "failed for distributor=%s",
                distributor.pk,
            )
            return render(
                request, "distributors/select_starter_pack.html", {"error": True}
            )

        return redirect(data["authorization_url"])

    return render(
        request,
        "distributors/select_starter_pack.html",
        {
            "pack_a_price": config.STARTER_PACK_A_PRICE,
            "pack_a_pv": config.STARTER_PACK_A_PV,
            "pack_b_price": config.STARTER_PACK_B_PRICE,
            "pack_b_pv": config.STARTER_PACK_B_PV,
        },
    )


def starter_pack_payment_callback(request):
    """The user's browser lands here after attempting starter-pack payment.
    By this point they're already logged in (from the registration-fee
    callback), so unlike registration_payment_callback this doesn't need
    to handle auth -- it's just a fast-path alongside the webhook, same
    idempotent consume function either way."""
    reference = request.GET.get("reference", "")
    if reference:
        consume_paid_starter_pack(reference)
    return render(request, "distributors/starter_pack_payment_callback.html")


@csrf_exempt
@require_POST
def paystack_webhook(request):
    """Source: https://paystack.com/docs/payments/webhooks/ -- must verify
    the x-paystack-signature header before acting, and must return 200 OK
    or Paystack retries the same event for up to 72 hours (so this handler
    must be idempotent, which every charge.success handler below is, and
    which process_transfer_webhook_task's own no-raise contract exists to
    preserve for transfer events too). One webhook URL handles every
    Paystack use case in this project (Paystack has no per-transaction
    webhook config), so it dispatches by event type, then by reference
    prefix -- "reg-" (Task 10b), "pack-" (Task 10c), "order-" (Task 17d)
    for charge.success, or "withdrawal-payout-" (Task 16f PR 3) below for
    transfer events. The prefix -> handler dict is built fresh inside
    this function (not module-level) so each entry resolves the current
    module-level name at call time -- a module-level dict would instead
    capture these function objects once at import time, silently
    defeating @patch("apps.distributors.views.consume_paid_starter_pack")
    -style test mocking, which every existing test for this dispatch
    already relies on.

    Transfer events (Task 16f PR 3, doubt-driven-development, 2026-07-24):
    confirmed against Paystack's own docs that transfer.success/failed/
    reversed use this same x-paystack-signature scheme, so the check
    above already covers them -- no separate signature path needed.
    Deferred to a Celery task (process_transfer_webhook_task) rather than
    handled inline, mirroring didit_webhook's own reasoning: verify_transfer
    alone can take up to REQUEST_TIMEOUT_SECONDS, which risks exceeding a
    webhook provider's response-time budget if called synchronously here."""
    signature = request.headers.get("x-paystack-signature", "")
    if not verify_webhook_signature(request.body, signature):
        return HttpResponseBadRequest("invalid signature")

    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("invalid JSON")

    event = payload.get("event")
    if event == "charge.success":
        reference = payload.get("data", {}).get("reference", "")
        # Task 17d added the third prefix (order-) the old if/elif's own
        # comment anticipated -- a prefix -> handler dict instead of more
        # branches, per that comment's own suggestion. Built here, not at
        # module level, so @patch("apps.distributors.views.consume_...")
        # -style test mocking still works (see this function's own
        # docstring for why a module-level dict would defeat that).
        charge_success_handlers = {
            "reg-": consume_paid_registration,
            "pack-": consume_paid_starter_pack,
            "order-": confirm_order_payment,
        }
        handler = next(
            (
                fn
                for prefix, fn in charge_success_handlers.items()
                if reference.startswith(prefix)
            ),
            None,
        )
        if handler:
            handler(reference)
        elif reference:
            logger.warning(
                "paystack_webhook: unrecognized reference prefix: %s", reference
            )
    elif event in TRANSFER_WEBHOOK_EVENTS:
        reference = payload.get("data", {}).get("reference", "")
        # doubt-driven-development finding, 2026-07-24: a blank reference
        # (e.g. an unexpected payload shape) must log the same as an
        # unrecognized prefix, not fall through silently -- both are
        # "this event didn't dispatch anywhere" and equally worth
        # noticing, not just the latter.
        if reference.startswith("withdrawal-payout-"):
            process_transfer_webhook_task.delay(reference)
        else:
            logger.warning(
                "paystack_webhook: unrecognized/missing transfer reference: %r",
                reference,
            )

    return HttpResponse(status=200)


@ratelimit(key="ip", rate="10/m", method="POST")
def verify_otp_view(request):
    phone_number = request.session.get("otp_phone_number")
    purpose = request.session.get("otp_purpose")
    if not phone_number or not purpose:
        return redirect("distributors:register")

    form = OTPVerificationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_otp(
            phone_number, purpose=purpose, submitted_code=form.cleaned_data["code"]
        ):
            del request.session["otp_phone_number"]
            del request.session["otp_purpose"]
            if purpose == "registration":
                distributor = Distributor.objects.select_related("user").get(
                    phone_number=phone_number
                )
                distributor.phone_verified = True
                distributor.save(update_fields=["phone_verified"])
                auth_login(request, distributor.user, backend=AUTH_BACKEND)
                return redirect("distributors:dashboard")
            # password_reset: hold the verified phone number for the next
            # step, but don't log the user in yet — they haven't set a new
            # password.
            request.session["reset_verified_phone_number"] = phone_number
            return redirect("distributors:set_new_password")
        form.add_error("code", "Incorrect or expired code.")

    return render(
        request,
        "distributors/verify_otp.html",
        {"form": form, "phone_number": phone_number},
    )


@require_POST
@ratelimit(key="ip", rate="5/h", method="POST")
def resend_otp(request):
    # require_POST closes a real gap: this view has a side effect (a real,
    # billed SMS send) but previously accepted any method — a bare GET
    # bypasses Django's CSRF check entirely (CSRF only applies to
    # state-changing methods), so an <img> tag or similar could trigger a
    # send while a victim had an in-progress OTP flow.
    phone_number = request.session.get("otp_phone_number")
    purpose = request.session.get("otp_purpose")
    if phone_number and purpose:
        generate_otp(phone_number, purpose=purpose)
    return redirect("distributors:verify_otp")


@ratelimit(key="ip", rate="20/m", method="POST")
def login_view(request):
    form = DistributorLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone_number = str(form.cleaned_data["phone_number"])
        result = attempt_distributor_login(phone_number, form.cleaned_data["password"])

        if result.locked:
            seconds_remaining = (result.locked_until - timezone.now()).total_seconds()
            minutes_remaining = max(1, math.ceil(seconds_remaining / 60))
            return render(
                request,
                "distributors/account_locked.html",
                {"minutes_remaining": minutes_remaining},
            )
        elif result.needs_verification:
            generate_otp(phone_number, purpose="registration")
            request.session["otp_phone_number"] = phone_number
            request.session["otp_purpose"] = "registration"
            return redirect("distributors:verify_otp")
        elif result.success:
            auth_login(request, result.user, backend=AUTH_BACKEND)
            return redirect("distributors:dashboard")
        else:
            form.add_error(None, "Incorrect phone number or password.")

    return render(request, "distributors/login.html", {"form": form})


def logout_view(request):
    auth_logout(request)
    return redirect("distributors:login")


@ratelimit(key="ip", rate="5/h", method="POST")
def forgot_password(request):
    form = DistributorForgotPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone_number = str(form.cleaned_data["phone_number"])
        if Distributor.objects.filter(phone_number=phone_number).exists():
            generate_otp(phone_number, purpose="password_reset")
        # Redirect the same way regardless of whether the account exists,
        # to avoid revealing which phone numbers are registered.
        request.session["otp_phone_number"] = phone_number
        request.session["otp_purpose"] = "password_reset"
        return redirect("distributors:verify_otp")
    return render(request, "distributors/forgot_password.html", {"form": form})


def set_new_password(request):
    phone_number = request.session.get("reset_verified_phone_number")
    if not phone_number:
        return redirect("distributors:forgot_password")

    form = DistributorSetNewPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        distributor = Distributor.objects.select_related("user").get(
            phone_number=phone_number
        )
        distributor.user.set_password(form.cleaned_data["password1"])
        distributor.user.save()
        del request.session["reset_verified_phone_number"]
        return redirect("distributors:reset_success")

    return render(request, "distributors/set_new_password.html", {"form": form})


def reset_success(request):
    return render(request, "distributors/reset_success.html")


@login_required(login_url="distributors:login")
@_redirect_if_cooling_off_cancelled
@ratelimit(key="user", rate="20/h", method="POST")
def start_kyc_verification(request):
    """Task 11b: redirects the distributor to Didit's hosted verification
    page (ID front/back + a live selfie -- face-match + liveness + document
    checks all happen on Didit's side). Phone-verified gate mirrors the old
    submit_kyc's. Once already approved by an admin, resubmission is
    blocked -- otherwise a distributor can restart as many times as needed
    regardless of what Didit itself said last time, since Didit's result is
    purely informational (Task 11c) and never sets kyc_status itself."""
    distributor = request.user.distributor

    if not distributor.phone_verified:
        return render(
            request,
            "distributors/start_kyc_verification.html",
            {"error": "Verify your phone number before starting KYC verification."},
        )

    if distributor.kyc_status == Distributor.KycStatus.APPROVED:
        return redirect("distributors:dashboard")

    if request.method == "POST":
        callback_url = request.build_absolute_uri(
            reverse("distributors:kyc_verification_callback")
        )
        try:
            session = create_didit_session(
                callback_url=callback_url, vendor_data=distributor.pk
            )
        except DiditError:
            logger.exception(
                "start_kyc_verification: Didit create_verification_session "
                "failed for distributor=%s",
                distributor.pk,
            )
            return render(
                request, "distributors/start_kyc_verification.html", {"error": True}
            )

        DiditVerification.objects.update_or_create(
            distributor=distributor,
            defaults={
                "session_id": session["session_id"],
                "status": DiditVerification.Status.PENDING,
            },
        )
        return redirect(session["url"])

    awaiting_review = (
        distributor.kyc_status == Distributor.KycStatus.PENDING
        and DiditVerification.objects.filter(distributor=distributor).exists()
    )
    return render(
        request,
        "distributors/start_kyc_verification.html",
        {"awaiting_review": awaiting_review},
    )


def kyc_verification_callback(request):
    """The distributor's browser lands here after finishing (or
    abandoning) Didit's hosted flow. Fast-path alongside the webhook --
    Didit's own callback query params are read only to know which session
    to re-check; consume_didit_result always re-fetches the authoritative
    result from Didit rather than trusting them."""
    session_id = request.GET.get("verificationSessionId", "")
    if session_id:
        consume_didit_result(session_id)
    return render(request, "distributors/kyc_verification_callback.html")


@csrf_exempt
@require_POST
def didit_webhook(request):
    """Signature scheme confirmed 2026-07-14 against Didit's own primary
    docs (docs.didit.me/integration/webhooks) -- X-Signature-V2, HMAC-SHA256
    over the canonical form of the whole payload, plus an X-Timestamp
    freshness check. See apps/distributors/didit.py::
    verify_webhook_signature's docstring for the full scheme and why this
    project's earlier "Simple" scheme was wrong (sourced from a secondary
    reference; Didit's own docs say "Simple"... "does NOT authenticate
    decision data"). Must return 200 OK once accepted (Didit retries
    otherwise), so this handler needs to be idempotent, which
    consume_didit_result is.

    Confirmed via the same docs page: a 5-second response timeout, 2
    retries, then dropped -- and an explicit instruction to return 2xx as
    soon as the work is queued. consume_didit_result does a Didit API call
    plus up to 3 synchronous image downloads, easily exceeding 5 seconds,
    so this enqueues it via Celery (consume_didit_result_task) instead of
    calling it inline."""
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("invalid JSON")

    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature-V2", "")

    if not verify_didit_webhook_signature(payload, timestamp, signature):
        return HttpResponseBadRequest("invalid signature")

    session_id = payload.get("session_id", "")
    if session_id:
        consume_didit_result_task.delay(session_id)

    return HttpResponse(status=200)


@login_required(login_url="distributors:login")
@_redirect_if_cooling_off_cancelled
def dashboard(request):
    """Task 20a: the real dashboard core stats (Section 6.1 of the primary
    source doc), computed against real, already-aggregated data sources --
    never a tree walk (CLAUDE.md's standing Scale Architecture warning).

    Same three-account-types gap earnings_history already guards against
    (Task 15d/security review 2026-07-22): @login_required alone doesn't
    distinguish a customer account from a distributor, so an unguarded
    request.user.distributor would raise an unhandled 500 for a logged-in
    customer instead of a clean 403.

    "Earnings" (total and this-week) is the three bonus-type
    WalletTransactions only (direct_referral_bonus/binary_bonus/
    matching_bonus) -- Section 6.1 says This Week's Earnings is
    "from all bonus types", and Total Earnings uses the same metric
    accumulated since joining for consistency. This mirrors
    apps/admin_portal/views.py's own existing platform-wide earnings
    aggregate (Task 23) exactly, including its single-conditional-
    aggregate-query shape rather than one query per bonus type -- that
    view already went through its own code-review pass establishing this
    as the right pattern.

    "This week" is the current ISO calendar week (Monday 00:00), not a
    rolling window like Matching Bonus's own MATCHING_BONUS_INTERVAL_DAYS
    -- confirmed against Section 6.1's own wording ("in the current
    week"), not assumed.

    Wallet balance / left-right leg PV are read via a single `.values()`
    projection across the (possibly-missing) Wallet/PvLedger OneToOne
    rows, not attribute access (`distributor.wallet`/`distributor.
    pv_ledger`) -- both are created lazily on first credit, so a
    brand-new distributor legitimately has neither yet, and naive
    attribute access would raise DoesNotExist instead of a safe 0. This
    is the exact same failure mode apps/binary_tree/services.py::
    get_ancestor_pv_aggregates already documents and avoids the same way.

    Team size counts the entire downline, both legs combined (Section
    6.1's own wording), via BinaryTreeEdge's closure table -- O(number of
    descendants) through the existing (ancestor, leg) index, not a
    recursive walk."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor

    row = (
        Distributor.objects.filter(pk=distributor.pk)
        .values("wallet__balance", "pv_ledger__left_leg_pv", "pv_ledger__right_leg_pv")
        .first()
    )
    wallet_balance = row["wallet__balance"] or Decimal("0")
    left_leg_pv = row["pv_ledger__left_leg_pv"] or 0
    right_leg_pv = row["pv_ledger__right_leg_pv"] or 0

    earnings_filter = Q(
        transaction_type__in=[
            WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            WalletTransaction.TransactionType.BINARY_BONUS,
            WalletTransaction.TransactionType.MATCHING_BONUS,
        ]
    )
    # Code review: computed straight off timezone.now() (UTC), not
    # timezone.localtime() -- correct today only because TIME_ZONE is
    # "UTC" (bancostore/settings.py) and Ghana has no offset from UTC.
    # Would need timezone.localtime(now) first if TIME_ZONE is ever
    # changed to something with a real offset.
    now = timezone.now()
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    earnings = WalletTransaction.objects.filter(
        wallet__distributor=distributor
    ).aggregate(
        total_earnings=Sum("amount", filter=earnings_filter),
        this_week_earnings=Sum(
            "amount", filter=earnings_filter & Q(created_at__gte=week_start)
        ),
    )
    total_earnings = earnings["total_earnings"] or Decimal("0")
    this_week_earnings = earnings["this_week_earnings"] or Decimal("0")

    monthly_personal_pv = (
        MonthlyPersonalPv.objects.filter(
            distributor=distributor, period=now.date().replace(day=1)
        )
        .values_list("pv", flat=True)
        .first()
        or 0
    )

    team_size = BinaryTreeEdge.objects.filter(ancestor=distributor).count()

    # Task 20c: real, not the mockup's hardcoded always-eligible state --
    # against the actual admin-editable MIN_MONTHLY_PERSONAL_PV setting.
    is_pv_eligible = monthly_personal_pv >= config.MIN_MONTHLY_PERSONAL_PV
    personal_pv_shortfall = max(0, config.MIN_MONTHLY_PERSONAL_PV - monthly_personal_pv)

    # Task 20c: real "resets in N days" derived from the same Monday-00:00
    # boundary this_week_earnings already uses, not a fabricated number.
    days_until_week_reset = 7 - now.weekday()

    # Task 20b: Section 6.1's "personal recruitment link" -- distributors:
    # register's GET handler already reads ?ref=<IR ID> and prefills
    # sponsor_ir_id (pre-existing, found untested while building this
    # slice). None until ir_id exists (KYC not yet approved) -- nothing
    # to share yet, so the template must not render a broken link.
    referral_url = None
    referral_message = None
    if distributor.ir_id:
        # CodeRabbit: ir_id is built from IR_ID_PREFIX, an unvalidated
        # free-text constance setting (same reasoning as the json_script
        # fix in dashboard.html) -- url-encode it rather than
        # concatenating raw, so a prefix containing &/#/%/whitespace
        # can't break the query string structure.
        referral_url = (
            request.build_absolute_uri(reverse("distributors:register"))
            + f"?ref={quote(distributor.ir_id, safe='')}"
        )
        referral_message = (
            f"Join Bancostore as a distributor using my referral link: {referral_url}"
        )

    return render(
        request,
        "distributors/dashboard.html",
        {
            "active_nav": "dashboard",
            "wallet_balance": wallet_balance,
            "total_earnings": total_earnings,
            "this_week_earnings": this_week_earnings,
            "left_leg_pv": left_leg_pv,
            "right_leg_pv": right_leg_pv,
            "monthly_personal_pv": monthly_personal_pv,
            "team_size": team_size,
            "ir_id": distributor.ir_id,
            "rank": distributor.rank,
            "full_name": distributor.full_name,
            "referral_url": referral_url,
            "referral_message": referral_message,
            "is_pv_eligible": is_pv_eligible,
            "personal_pv_shortfall": personal_pv_shortfall,
            "min_monthly_personal_pv": config.MIN_MONTHLY_PERSONAL_PV,
            "days_until_week_reset": days_until_week_reset,
        },
    )


@login_required(login_url="distributors:login")
@_redirect_if_cooling_off_cancelled
def earnings_history(request):
    """Task 15d: a distributor's own wallet ledger. Always scoped to
    request.user.distributor -- no distributor id is ever accepted from the
    URL or query params, so there is no IDOR surface here to guard against.

    Security review (2026-07-22) caught that this platform has three
    account types sharing one User model (customer/distributor/admin) and
    @login_required alone doesn't distinguish them -- an authenticated
    customer has no Distributor row, so request.user.distributor would
    raise an unhandled 500 instead of a clean 403. is_distributor() guards
    that explicitly here.

    Wallet.balance is a cached aggregate, not re-derived from the ledger on
    every request (apps/wallet/models.py already treats it as the source of
    truth for "current balance"); total_earned/total_withdrawn below are
    ledger sums shown alongside it purely for the summary strip."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor
    transactions = WalletTransaction.objects.filter(
        wallet__distributor=distributor
    ).order_by("-created_at", "-pk")

    totals = transactions.aggregate(
        total_earned=Sum("amount", filter=Q(amount__gt=0)),
        total_withdrawn=Sum(
            "amount",
            filter=Q(
                transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT
            ),
        ),
        last_withdrawal_at=Max(
            "created_at",
            filter=Q(
                transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT
            ),
        ),
    )
    total_earned = totals["total_earned"] or Decimal("0")
    total_withdrawn = -(totals["total_withdrawn"] or Decimal("0"))
    last_withdrawal_at = totals["last_withdrawal_at"]

    # OneToOneField's reverse accessor raises Wallet.DoesNotExist (an
    # AttributeError subclass) until the wallet is lazily created by a
    # first credit() -- getattr's default handles a distributor with no
    # earnings yet.
    wallet = getattr(distributor, "wallet", None)
    balance = wallet.balance if wallet else Decimal("0")

    paginator = Paginator(transactions, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "distributors/earnings_history.html",
        {
            "page_obj": page_obj,
            "balance": balance,
            "total_earned": total_earned,
            "total_withdrawn": total_withdrawn,
            "last_withdrawal_at": last_withdrawal_at,
            "active_nav": "earnings_history",
        },
    )


@login_required(login_url="distributors:login")
@_redirect_if_cooling_off_cancelled
def binary_tree_view(request):
    """Task 21a: a distributor's own downline as a visual tree (Section 6.2
    of the primary source doc). Always scoped to request.user.distributor --
    no distributor id is ever accepted from the URL or query params, so
    there is no IDOR surface to guard against, same as earnings_history.

    Same three-account-types gap earnings_history/dashboard already guard
    against: is_distributor() first, since @login_required alone doesn't
    distinguish a customer account from a distributor.

    The tree itself is built by apps.binary_tree.services.get_downline_tree,
    which is exactly three queries regardless of downline size or depth --
    never a recursive walk (SPEC.md Scale Architecture)."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor
    tree = get_downline_tree(distributor)

    def _count_subtree(node):
        return 1 + sum(_count_subtree(child) for child in node.children)

    left_root = next(
        (c for c in tree.children if c.leg == BinaryTreeEdge.Leg.LEFT), None
    )
    right_root = next(
        (c for c in tree.children if c.leg == BinaryTreeEdge.Leg.RIGHT), None
    )

    return render(
        request,
        "distributors/binary_tree.html",
        {
            "tree": tree,
            "left_team_count": _count_subtree(left_root) if left_root else 0,
            "right_team_count": _count_subtree(right_root) if right_root else 0,
            "active_nav": "binary_tree",
        },
    )


@login_required(login_url="distributors:login")
def withdrawal_history(request):
    """Task 16g: a distributor's own withdrawal request history and
    current status. Always scoped to request.user.distributor -- same
    is_distributor() + no id/param IDOR surface as earnings_history/
    payout_settings/withdrawal_request (Task 15d/16a/16c) -- CLAUDE.md's
    own "Deferred, not silently skipped" note on Task 15 flagged this
    exact unguarded-request.user.distributor pattern as still open in
    three other views; this one is built correctly from the start.

    Ordered -created_at, -pk (not -created_at alone) -- Task 15d's own
    pagination bug (a timestamp collision could skip/duplicate a row
    across a page boundary with no tie-breaker) is a mistake worth not
    repeating here."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor
    requests_qs = WithdrawalRequest.objects.filter(distributor=distributor).order_by(
        "-created_at", "-pk"
    )

    paginator = Paginator(requests_qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "distributors/withdrawal_history.html",
        {
            "page_obj": page_obj,
            "active_nav": "withdrawal_history",
        },
    )


@login_required(login_url="distributors:login")
@ratelimit(key="user", rate="20/h", method="POST")
def payout_settings(request):
    """Task 16a: where a distributor sets the mobile money number/network
    Paystack Transfer pays out to (Task 16). Always scoped to
    request.user.distributor -- same is_distributor() + no id/param IDOR
    surface as earnings_history (Task 15d). Rate-limited on POST, matching
    every other state-changing endpoint in this file (register, login,
    select_starter_pack) -- this one changes where real money gets sent.

    Uses a ?saved=1 redirect param for the success banner rather than
    django.contrib.messages -- messages is installed project-wide but has
    no consumer yet, and wiring it into the shared base_dashboard.html
    shell for one page is more than this task needs."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor

    if request.method == "POST":
        form = PayoutSettingsForm(request.POST)
        if form.is_valid():
            distributor.mobile_money_number = str(
                form.cleaned_data["mobile_money_number"]
            )
            distributor.mobile_money_network = form.cleaned_data["mobile_money_network"]
            distributor.save(
                update_fields=["mobile_money_number", "mobile_money_network"]
            )
            return redirect(f"{reverse('distributors:payout_settings')}?saved=1")
    else:
        initial = {}
        if distributor.has_payout_destination:
            initial = {
                "mobile_money_number": distributor.mobile_money_number,
                "mobile_money_network": distributor.mobile_money_network,
            }
        form = PayoutSettingsForm(initial=initial)

    # The mobile money network field is a custom Alpine-driven listbox, not
    # a rendered <select> -- both its initial label AND its initial
    # selected value must be server-rendered too (not left to Alpine's
    # x-text/x-data alone), or a no-JS request / the Django test client
    # (which never executes JS) would see the placeholder text even when a
    # network is already saved.
    #
    # security-and-hardening (2026-07-22, code-review-and-quality pass):
    # the first version of this interpolated form.mobile_money_network.value
    # -- the raw, unvalidated client-submitted string -- directly into the
    # Alpine x-data JS expression. Django's HTML autoescaping does NOT
    # protect that: the browser HTML-decodes the attribute value (turning
    # &#x27; back into ') BEFORE Alpine evaluates it as JS, so a crafted
    # mobile_money_network POST value could break out of the JS string
    # literal and execute arbitrary script in the distributor's own
    # authenticated session -- on the exact page that controls where their
    # withdrawal money gets sent. Fixed two ways at once: (1) the value is
    # constrained to the known-good MobileMoneyNetwork choices right here,
    # server-side, before it ever reaches the template -- an invalid/
    # injected value simply becomes "" (never selected), so there is no
    # channel left to smuggle arbitrary content through; (2) both the
    # constrained value and the full choices list are passed to the
    # template via `json_script` (templates/distributors/payout_settings
    # .html), which safely serializes into a <script type="application/
    # json"> block for Alpine to JSON.parse -- never interpolated into a
    # JS-evaluated attribute string. This also fixes a separate, lower-
    # severity issue the same review caught: the network choices were
    # hardcoded a second and third time (once in the Alpine options array,
    # once in this view's old dict-lookup) -- now there is exactly one
    # source, Distributor.MobileMoneyNetwork.choices, read once below.
    valid_networks = dict(Distributor.MobileMoneyNetwork.choices)
    submitted_network = form.data.get("mobile_money_network") if form.is_bound else ""
    if form.is_bound:
        selected_network = (
            submitted_network if submitted_network in valid_networks else ""
        )
    elif distributor.has_payout_destination:
        selected_network = distributor.mobile_money_network
    else:
        selected_network = ""
    network_display = valid_networks.get(selected_network, "")
    network_choices = [
        {"value": value, "label": label}
        for value, label in Distributor.MobileMoneyNetwork.choices
    ]

    return render(
        request,
        "distributors/payout_settings.html",
        {
            "form": form,
            "distributor": distributor,
            "network_display": network_display,
            "selected_network": selected_network,
            "network_choices": network_choices,
            "saved": request.GET.get("saved") == "1",
            "active_nav": "payout_settings",
        },
    )


# Maps each apps.withdrawal.services rejection exception to a distinct,
# distributor-facing message (Task 16c acceptance criterion) -- kept as
# one lookup table rather than a chain of except blocks with repeated
# render() calls, so adding a new rejection reason later is a one-line
# addition here, not a new branch shaped like all the others.
_WITHDRAWAL_ERROR_MESSAGES = {
    KycNotApproved: "Your KYC must be approved before you can request a withdrawal.",
    PayoutDestinationNotSet: (
        "Please set your payout destination in Settings before requesting "
        "a withdrawal."
    ),
    BelowMinimumAmount: "The amount is below the minimum withdrawal amount.",
    AboveMaximumAmount: "The amount is above the maximum withdrawal amount.",
    InsufficientWalletBalance: "Your wallet balance is not enough for this withdrawal.",
    WithdrawalWindowActive: (
        "You've already requested a withdrawal recently. Please try again later."
    ),
    # code-review-and-quality (2026-07-23): these two are admin
    # misconfiguration, not something the distributor caused or can fix
    # -- same generic message for both, logged with the real cause
    # server-side by submit_withdrawal_request itself before it raises.
    WithdrawalFrequencyMisconfigured: (
        "Withdrawals are temporarily unavailable. Please try again later."
    ),
    WithholdingTaxMisconfigured: (
        "Withdrawals are temporarily unavailable. Please try again later."
    ),
}


@login_required(login_url="distributors:login")
@ratelimit(key="user", rate="20/h", method="POST")
def withdrawal_request(request):
    """Task 16c. Always scoped to request.user.distributor -- same
    is_distributor() + no id/param IDOR surface as earnings_history/
    payout_settings. submit_withdrawal_request (apps.withdrawal.services)
    is the sole authority on every rejection reason; this view only maps
    its exceptions to distributor-facing text, never re-implements any of
    the checks itself."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor
    wallet = getattr(distributor, "wallet", None)
    balance = wallet.balance if wallet else Decimal("0")
    error = None

    if request.method == "POST":
        form = WithdrawalRequestForm(request.POST)
        if form.is_valid():
            try:
                submit_withdrawal_request(distributor, form.cleaned_data["amount"])
            except tuple(_WITHDRAWAL_ERROR_MESSAGES) as exc:
                error = _WITHDRAWAL_ERROR_MESSAGES[type(exc)]
            else:
                return redirect(
                    f"{reverse('distributors:withdrawal_request')}?submitted=1"
                )
    else:
        form = WithdrawalRequestForm()

    return render(
        request,
        "distributors/withdrawal_request.html",
        {
            "form": form,
            "balance": balance,
            "error": error,
            "submitted": request.GET.get("submitted") == "1",
            "min_amount": config.MIN_WITHDRAWAL_AMOUNT,
            "max_amount": config.MAX_WITHDRAWAL_AMOUNT,
            "tax_rate": config.WITHHOLDING_TAX_RATE,
            "active_nav": "withdrawal_request",
            "cooling_off_cancelled": distributor.cooling_off_cancelled_at is not None,
        },
    )


@login_required(login_url="distributors:login")
@ratelimit(key="user", rate="20/h", method="POST")
def cancel_membership(request):
    """Task 19c: the distributor-facing "Cancel Membership & Request
    Refund" screen (Section 9's 7-day cooling-off right, ADR-0007). Same
    is_distributor() + no id/param IDOR surface as earnings_history/
    payout_settings -- always request.user.distributor, never an id from
    the URL or form. All eligibility/refund-math logic lives in
    apps.distributors.cooling_off_services; this view only renders it and
    maps its exceptions to a redirect back to this same page (which will
    then show the ineligible state on the next GET) -- it never
    re-implements any of the checks itself.

    POST calls cancel_membership_and_refund, which deactivates
    request.user (user.is_active = False) as its very last step -- this
    view then explicitly logs the now-stale session out itself
    (auth_logout) rather than relying on is_active alone, since Django
    does not automatically invalidate an already-authenticated session
    when is_active flips to False mid-session (same characteristic
    apps/admin_portal's suspend toggle already has). The refund amount is
    rendered directly in this same response, not carried via a redirect
    or django.contrib.messages -- logout() flushes the session, which
    would silently drop a session-backed message before the distributor
    ever saw it."""
    if not is_distributor(request.user):
        raise PermissionDenied

    distributor = request.user.distributor

    if request.method == "POST":
        try:
            refund_amount = cancel_membership_and_refund(distributor.pk)
        except (CoolingOffPeriodExpired, NoRefundableStarterPackPurchase):
            return redirect("distributors:cancel_membership")

        auth_logout(request)
        return render(
            request,
            "distributors/membership_cancelled.html",
            {"refund_amount": refund_amount},
        )

    now = timezone.now()
    has_purchase = distributor.starter_pack_confirmed_at is not None
    already_cancelled = distributor.cooling_off_cancelled_at is not None
    deadline = (
        distributor.starter_pack_confirmed_at
        + timedelta(days=config.COOLING_OFF_PERIOD_DAYS)
        if has_purchase
        else None
    )
    eligible = has_purchase and not already_cancelled and now <= deadline

    context = {"eligible": eligible, "active_nav": "payout_settings"}
    if eligible:
        price = Decimal(distributor.starter_pack_price_pesewas) / Decimal("100")
        refund_amount = calculate_cooling_off_refund(
            distributor.starter_pack_price_pesewas
        )
        context.update(
            {
                "distributor": distributor,
                "price": price,
                "fee_amount": price - refund_amount,
                "refund_amount": refund_amount,
                "deduction_rate": config.COOLING_OFF_REFUND_DEDUCTION_RATE,
                "registration_fee": config.REGISTRATION_FEE,
                "days_remaining": max(0, (deadline - now).days),
            }
        )

    return render(request, "distributors/cancel_membership.html", context)
