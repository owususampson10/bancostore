import ipaddress
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import AbstractBaseUser, Group
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

import requests
from constance import config

from apps.binary_tree.services import AlreadyPlacedError, BinaryTree
from apps.commissions.services import calculate_direct_referral_bonus
from apps.notifications.sms import send_sms
from apps.pv_ledger.services import record_purchase_pv
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit as credit_wallet
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)
from bancostore.media import resize_and_convert_to_webp

from .didit import DiditError, get_session_decision
from .models import DiditVerification, Distributor, IrIdSequence, PendingRegistration
from .paystack import PaystackError, verify_transaction

logger = logging.getLogger(__name__)

User = get_user_model()


class PendingRegistrationNotFound(Exception):
    pass


class IrIdSequenceExhausted(Exception):
    pass


class PendingRegistrationAlreadyConsumed(Exception):
    pass


class StarterPackAlreadyConfirmed(Exception):
    pass


@dataclass
class LoginAttempt:
    success: bool
    locked: bool = False
    locked_until: datetime | None = None
    needs_verification: bool = False
    user: AbstractBaseUser | None = None


def attempt_distributor_login(phone_number: str, password: str) -> LoginAttempt:
    """Phone+password login with wrong-password lockout, using
    django-constance thresholds (MAX_FAILED_LOGIN_ATTEMPTS,
    ACCOUNT_LOCKOUT_DURATION_MINUTES) rather than hardcoded numbers.

    An already-locked account only reports locked=True if the submitted
    password is actually correct. Checking lock state before the password
    would let anyone who knows/guesses a phone number confirm it's locked —
    and therefore a real, currently-targeted account — without ever
    needing to know the real password.

    The counter read-modify-write (increment on failure / reset on
    success) is wrapped in select_for_update() + retry_on_lock_contention()
    — a real multi-threaded test reproduced a lost-update race here under
    concurrent failed logins before this fix (see
    tests/feature/distributors/test_distributor_auth.py::
    test_concurrent_failed_logins_do_not_lose_increments)."""
    try:
        distributor = Distributor.objects.select_related("user").get(
            phone_number=phone_number
        )
    except Distributor.DoesNotExist:
        return LoginAttempt(success=False)

    now = timezone.now()
    if distributor.locked_until and distributor.locked_until > now:
        if distributor.user.check_password(password):
            return LoginAttempt(
                success=False, locked=True, locked_until=distributor.locked_until
            )
        return LoginAttempt(success=False)

    # authenticate() does its own (slow, bcrypt-style) password check —
    # deliberately done outside any lock, both here and for the counter
    # update below, so a lock isn't held for the duration of a hash
    # comparison.
    user = authenticate(phone_number=phone_number, password=password)

    if user is None:

        def _record_failure():
            with transaction.atomic():
                locked = select_for_update_nowait_if_supported(Distributor.objects).get(
                    pk=distributor.pk
                )
                lock_now = timezone.now()
                if locked.locked_until and locked.locked_until <= lock_now:
                    # Lock had expired since the initial fetch — fresh
                    # attempts.
                    locked.failed_login_attempts = 0
                    locked.locked_until = None
                locked.failed_login_attempts += 1
                if locked.failed_login_attempts >= config.MAX_FAILED_LOGIN_ATTEMPTS:
                    locked.locked_until = lock_now + timedelta(
                        minutes=config.ACCOUNT_LOCKOUT_DURATION_MINUTES
                    )
                locked.save(update_fields=["failed_login_attempts", "locked_until"])
            return locked

        updated = retry_on_lock_contention(_record_failure)
        return LoginAttempt(
            success=False,
            locked=bool(updated.locked_until),
            locked_until=updated.locked_until,
        )

    def _record_success():
        with transaction.atomic():
            locked = select_for_update_nowait_if_supported(Distributor.objects).get(
                pk=distributor.pk
            )
            locked.failed_login_attempts = 0
            locked.locked_until = None
            locked.save(update_fields=["failed_login_attempts", "locked_until"])
        return locked

    retry_on_lock_contention(_record_success)

    if not distributor.phone_verified:
        return LoginAttempt(success=False, needs_verification=True, user=user)

    return LoginAttempt(success=True, user=user)


def snapshot_payment_reference(token) -> PendingRegistration:
    """Task 10b: atomically regenerates PendingRegistration.payment_reference
    and snapshots the current registration fee. Locked (matching this
    project's existing convention) so two concurrent requests for the same
    token -- a double-click, two open tabs -- can't overwrite each other's
    reference: the loser's reference would silently stop matching anything,
    and a customer who then pays via that now-orphaned checkout page would
    have no way to complete their registration."""

    def _attempt():
        with transaction.atomic():
            try:
                pending = select_for_update_nowait_if_supported(
                    PendingRegistration.objects.filter(token=token)
                ).get()
            except PendingRegistration.DoesNotExist:
                raise PendingRegistrationNotFound from None
            if pending.consumed_at is not None:
                raise PendingRegistrationAlreadyConsumed
            pending.fee_amount_pesewas = int(config.REGISTRATION_FEE * 100)
            pending.payment_reference = (
                f"reg-{pending.token.hex}-{uuid.uuid4().hex[:8]}"
            )
            pending.save(update_fields=["fee_amount_pesewas", "payment_reference"])
            return pending

    return retry_on_lock_contention(_attempt)


def consume_paid_registration(reference: str) -> None:
    """Task 10b: the single source of truth for turning a paid
    PendingRegistration into a real account. Called from BOTH the Paystack
    webhook and the callback-redirect view -- whichever arrives first
    completes it; both call sites, and repeat calls with the same
    reference, are always safe (idempotent). Design confirmed via
    doubt-driven-development 2026-07-13 -- see the
    project_paystack_registration_payment_design memory.

    Never trusts a caller's claims about payment status/amount/currency --
    always re-verifies server-side against Paystack's authoritative
    /transaction/verify endpoint before creating anything. Never raises:
    every failure path logs and returns, since a webhook handler crashing
    just means Paystack retries the same request for up to 72 hours.
    """

    def _attempt():
        with transaction.atomic():
            try:
                pending = select_for_update_nowait_if_supported(
                    PendingRegistration.objects.filter(payment_reference=reference)
                ).get()
            except PendingRegistration.DoesNotExist:
                logger.error(
                    "consume_paid_registration: no PendingRegistration found for "
                    "reference=%s -- a payment may have been confirmed with no "
                    "matching record (cleaned up, or a reference mismatch). "
                    "Needs manual investigation.",
                    reference,
                )
                return

            if pending.consumed_at is not None:
                return  # Already consumed -- idempotent no-op.

            try:
                verified = verify_transaction(reference)
            except PaystackError:
                logger.exception(
                    "consume_paid_registration: Paystack verify_transaction "
                    "failed for reference=%s",
                    reference,
                )
                return

            if verified.get("status") != "success":
                return
            if verified.get("currency") != "GHS":
                logger.warning(
                    "consume_paid_registration: unexpected currency %r for "
                    "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return
            if verified.get("amount") != pending.fee_amount_pesewas:
                logger.warning(
                    "consume_paid_registration: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    pending.fee_amount_pesewas,
                )
                return

            if Distributor.objects.filter(phone_number=pending.phone_number).exists():
                logger.error(
                    "consume_paid_registration: a Distributor with phone=%s "
                    "already exists -- refusing to create a duplicate for "
                    "reference=%s",
                    pending.phone_number,
                    reference,
                )
                return

            try:
                user = User(username=str(pending.phone_number), email=pending.email)
                # Already hashed in Task 10a via make_password() -- assigning
                # directly avoids double-hashing it through set_password().
                user.password = pending.password_hash
                user.save()
                distributor_group, _ = Group.objects.get_or_create(name="distributor")
                user.groups.add(distributor_group)
                Distributor.objects.create(
                    user=user,
                    phone_number=pending.phone_number,
                    full_name=pending.full_name,
                    address=pending.address,
                    area=pending.area,
                    landmark=pending.landmark,
                    sponsor=pending.sponsor,
                )
            except IntegrityError:
                logger.exception(
                    "consume_paid_registration: IntegrityError creating "
                    "account for reference=%s (likely a duplicate phone/"
                    "username race)",
                    reference,
                )
                return

            pending.consumed_at = timezone.now()
            pending.save(update_fields=["consumed_at"])

    retry_on_lock_contention(_attempt)


def snapshot_starter_pack_choice(distributor_pk, choice: str) -> Distributor:
    """Task 10c: atomically pins the price/PV/rank for the chosen starter
    pack (from django-constance, at selection time -- never re-derived
    live at confirmation time) and generates a fresh Paystack reference.
    Locked for the same reason as snapshot_payment_reference: two
    concurrent requests for the same distributor must not overwrite each
    other's reference. The constance read lives inside the retried
    transaction too -- a cold constance cache falls back to a real DB
    query (constance.backends.database), which under SQLite's single
    writer lock can itself raise "database is locked" under concurrent
    threads if left unprotected, the same class of issue fixed in Task
    10a's PvLedger creation."""

    def _attempt():
        with transaction.atomic():
            if choice == "A":
                price, pv, rank = (
                    config.STARTER_PACK_A_PRICE,
                    config.STARTER_PACK_A_PV,
                    config.STARTER_PACK_A_RANK,
                )
            else:
                price, pv, rank = (
                    config.STARTER_PACK_B_PRICE,
                    config.STARTER_PACK_B_PV,
                    config.STARTER_PACK_B_RANK,
                )
            distributor = select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor_pk)
            ).get()
            if distributor.starter_pack_confirmed_at is not None:
                raise StarterPackAlreadyConfirmed
            distributor.starter_pack_choice = choice
            distributor.starter_pack_price_pesewas = int(price * 100)
            distributor.starter_pack_pv = pv
            distributor.starter_pack_rank = rank
            distributor.starter_pack_payment_reference = (
                f"pack-{distributor.pk}-{uuid.uuid4().hex[:8]}"
            )
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

    return retry_on_lock_contention(_attempt)


def consume_paid_starter_pack(reference: str) -> None:
    """Task 10c/10d: mirrors consume_paid_registration's idempotent,
    server-verified pattern. Sets rank from the snapshotted
    starter_pack_rank (never re-derived from constance at confirmation
    time) once Paystack confirms the exact pinned amount was paid in GHS.

    Task 10d: this is also the distributor's first-ever binary tree
    placement (place_distributor is never called anywhere else) and the
    write-time PV credit up the ancestor chain -- both happen inside the
    same locked, idempotency-checked block as the rank change, so a
    webhook/callback race can't double-place or double-credit PV. Leg
    choice is always auto-balance (leg=None): Task 10a's registration
    form has no field for a sponsor to pick an explicit leg, so the
    "sponsor picks, or auto-balance falls back" design only exercises the
    fallback path today (confirmed with the user 2026-07-13)."""

    def _attempt():
        with transaction.atomic():
            try:
                distributor = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(starter_pack_payment_reference=reference)
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "consume_paid_starter_pack: no Distributor found for "
                    "reference=%s -- a payment may have been confirmed with "
                    "no matching record. Needs manual investigation.",
                    reference,
                )
                return

            if distributor.starter_pack_confirmed_at is not None:
                return  # Already consumed -- idempotent no-op.

            try:
                verified = verify_transaction(reference)
            except PaystackError:
                logger.exception(
                    "consume_paid_starter_pack: Paystack verify_transaction "
                    "failed for reference=%s",
                    reference,
                )
                return

            if verified.get("status") != "success":
                return
            if verified.get("currency") != "GHS":
                logger.warning(
                    "consume_paid_starter_pack: unexpected currency %r for "
                    "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return
            if verified.get("amount") != distributor.starter_pack_price_pesewas:
                logger.warning(
                    "consume_paid_starter_pack: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    distributor.starter_pack_price_pesewas,
                )
                return

            try:
                BinaryTree.place_distributor(distributor.sponsor, distributor, leg=None)
            except AlreadyPlacedError:
                logger.warning(
                    "consume_paid_starter_pack: distributor=%s was already "
                    "placed in the binary tree before starter-pack "
                    "confirmation -- unexpected given the current call "
                    "graph (place_distributor has no other caller), but "
                    "proceeding safely; PV is still credited below.",
                    distributor.pk,
                )

            record_purchase_pv(distributor, distributor.starter_pack_pv)

            if distributor.sponsor_id:
                # Task 12b: instant credit, same locked/idempotent block as
                # everything else here so a webhook/callback race can't
                # double-credit -- root-of-tree distributors have no
                # sponsor and simply don't generate this bonus.
                bonus = calculate_direct_referral_bonus(distributor.starter_pack_pv)
                credit_wallet(
                    distributor.sponsor,
                    bonus,
                    transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
                    reference=reference,
                )
                # Task 12c: the bonus has already landed -- an SMS provider
                # outage must never roll back money that was correctly
                # credited, so this is best-effort and never propagates.
                referred_name = distributor.full_name or str(distributor.phone_number)
                try:
                    send_sms(
                        str(distributor.sponsor.phone_number),
                        f"You've earned GHS {bonus} Direct Referral Bonus from "
                        f"{referred_name}'s purchase. Check your Bancostore wallet!",
                    )
                except Exception:
                    logger.exception(
                        "consume_paid_starter_pack: failed to notify sponsor=%s "
                        "of their GHS %s direct referral bonus -- credit already "
                        "applied, notification only.",
                        distributor.sponsor_id,
                        bonus,
                    )

            distributor.rank = distributor.starter_pack_rank
            distributor.starter_pack_confirmed_at = timezone.now()
            distributor.save(update_fields=["rank", "starter_pack_confirmed_at"])

    retry_on_lock_contention(_attempt)


# Didit's own overall session status -> our Status choices. Any other value
# (e.g. "Not Started"/"In Progress") means the hosted flow isn't finished
# yet -- nothing final to store.
_DIDIT_STATUS_MAP = {
    "Approved": DiditVerification.Status.APPROVED,
    "Declined": DiditVerification.Status.DECLINED,
    "In Review": DiditVerification.Status.IN_REVIEW,
}


# Confirmed 2026-07-14 against a real live Didit verification session: Didit
# does NOT serve document/selfie images from a didit.me (sub)domain -- it
# redirects to a specific S3 bucket it controls. Allowing the exact host
# (not a broad *.amazonaws.com suffix, which anyone can get a bucket on)
# keeps this an allowlist rather than reopening the SSRF hole this guard
# exists to close.
_ALLOWED_MEDIA_HOSTS = frozenset(
    {
        "didit.me",
        "service-didit-verification-production-a1c5f9b8.s3.amazonaws.com",
    }
)
_ALLOWED_MEDIA_HOST_SUFFIX = ".didit.me"


def _assert_safe_media_url(url):
    """SSRF guard: these URLs come from Didit's own decision response, not
    directly from a user-typed field, but the server still shouldn't
    blindly fetch whatever string appears there -- a Didit-side bug, a
    MITM, or a compromised session could otherwise point this at an
    internal service (cloud metadata, localhost, a private IP). Requires
    https, an exact match against _ALLOWED_MEDIA_HOSTS (or a didit.me
    subdomain), and a resolved IP that's actually public."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing to fetch non-https media URL: {url!r}")
    hostname = parsed.hostname or ""
    if hostname not in _ALLOWED_MEDIA_HOSTS and not hostname.endswith(
        _ALLOWED_MEDIA_HOST_SUFFIX
    ):
        raise ValueError(
            f"refusing to fetch media URL from unexpected host: {hostname!r}"
        )
    try:
        resolved_ip = ipaddress.ip_address(socket.gethostbyname(hostname))
    except (socket.gaierror, ValueError) as exc:
        raise ValueError(f"could not resolve media URL host: {hostname!r}") from exc
    if not resolved_ip.is_global:
        raise ValueError(
            f"refusing to fetch media URL resolving to a non-public IP: {resolved_ip}"
        )


def _download_image(url):
    """Fetch an image from one of Didit's short-lived media URLs and wrap
    it in a ContentFile that resize_and_convert_to_webp can read (it just
    needs something Image.open() accepts plus a .name)."""
    _assert_safe_media_url(url)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    name = url.split("?")[0].rsplit("/", 1)[-1] or "image.jpg"
    return ContentFile(response.content, name=name)


def _apply_decision_to_verification(verification, decision, session_id) -> bool:
    """Parses a Didit decision response and updates `verification`'s fields
    in place (caller is responsible for `.save()`). Returns False if
    nothing should be stored yet (still in progress, or the response
    couldn't be parsed), True once fields have been applied.

    Didit's response is third-party data -- untrusted shape, not just
    untrusted content. The parsing below assumes id_verifications/
    face_matches/liveness_checks are lists of dicts (per Didit's
    documented response, confirmed 2026-07-14 against a real live
    verification session), but a malformed or unexpected response (a bug
    on Didit's side, or a future field-name change) could still make any
    of these something else entirely. Catching broadly here keeps
    consume_didit_result's "never raises" contract (matching
    consume_paid_starter_pack/consume_paid_registration) even against a
    response shape we didn't anticipate. Never logs the decision payload
    itself -- it carries extracted PII (name, document number, date of
    birth)."""
    mapped_status = _DIDIT_STATUS_MAP.get(decision.get("status"))
    if mapped_status is None:
        return False  # Still in progress -- nothing final to store yet.

    try:
        id_verification = (decision.get("id_verifications") or [{}])[0]
        face_match = (decision.get("face_matches") or [{}])[0]
        liveness_check = (decision.get("liveness_checks") or [{}])[0]
        if not all(
            isinstance(x, dict) for x in (id_verification, face_match, liveness_check)
        ):
            raise TypeError("expected dict entries in Didit's decision response")
    except (TypeError, KeyError, IndexError):
        logger.exception(
            "consume_didit_result: unexpected decision response shape for "
            "session_id=%s -- cannot parse. Needs manual investigation.",
            session_id,
        )
        return False

    verification.status = mapped_status
    verification.id_verification_status = id_verification.get("status", "")
    verification.face_match_status = face_match.get("status", "")
    verification.face_match_score = face_match.get("score")
    verification.liveness_status = liveness_check.get("status", "")
    verification.liveness_score = liveness_check.get("score")
    verification.extracted_full_name = id_verification.get("full_name") or ""
    verification.extracted_document_number = (
        id_verification.get("document_number") or ""
    )
    verification.warnings = decision.get("warnings") or []

    dob_raw = id_verification.get("date_of_birth")
    if dob_raw:
        try:
            verification.extracted_date_of_birth = datetime.strptime(
                dob_raw, "%Y-%m-%d"
            ).date()
        except ValueError:
            # Never log dob_raw itself -- it's PII (a real date of birth),
            # and the docstring above promises this function never logs
            # decision-payload PII.
            logger.warning(
                "consume_didit_result: unexpected date_of_birth format for "
                "session_id=%s (value omitted, contains PII)",
                session_id,
            )

    # front_image/back_image/portrait_image field names confirmed 2026-07-14
    # against a real live Didit verification session; a missing key just
    # means no image gets stored, it doesn't crash.
    for field_name, url_key in (
        ("id_front_image", "front_image"),
        ("id_back_image", "back_image"),
        ("selfie_image", "portrait_image"),
    ):
        url = id_verification.get(url_key)
        if not url:
            continue
        try:
            downloaded = _download_image(url)
        except (requests.RequestException, ValueError):
            logger.exception(
                "consume_didit_result: failed to download %s for " "session_id=%s",
                url_key,
                session_id,
            )
            continue
        setattr(verification, field_name, resize_and_convert_to_webp(downloaded))

    return True


def consume_didit_result(session_id: str) -> None:
    """Task 11b: the single source of truth for turning a completed Didit
    verification session into a stored `DiditVerification` result. Called
    from both the callback-redirect view and the webhook -- whichever
    arrives first completes it; both call sites, and repeat calls with the
    same session_id, are always safe (idempotent).

    Never trusts a caller's claims about the result -- always re-fetches
    via get_session_decision() before storing anything, the same "always
    re-verify server-side" rule already applied to Paystack. Purely
    informational: never touches `Distributor.kyc_status` (only an admin's
    explicit action does, Task 11c -- see SPEC.md's "never auto-approve
    KYC" boundary).
    """

    def _attempt():
        with transaction.atomic():
            try:
                verification = select_for_update_nowait_if_supported(
                    DiditVerification.objects.filter(session_id=session_id)
                ).get()
            except DiditVerification.DoesNotExist:
                logger.error(
                    "consume_didit_result: no DiditVerification found for "
                    "session_id=%s -- a result may have arrived with no "
                    "matching record. Needs manual investigation.",
                    session_id,
                )
                return

            if verification.status != DiditVerification.Status.PENDING:
                return  # Already consumed -- idempotent no-op.

            try:
                decision = get_session_decision(session_id)
            except DiditError:
                logger.exception(
                    "consume_didit_result: Didit get_session_decision failed "
                    "for session_id=%s",
                    session_id,
                )
                return

            if _apply_decision_to_verification(verification, decision, session_id):
                verification.save()
                # Significant business event -- the on-call question this
                # answers: "did this distributor's KYC verification finish,
                # and with what result?" Never logs PII (name, document
                # number, DOB) -- just identifiers and the outcome.
                logger.info(
                    "consume_didit_result: session_id=%s distributor_id=%s "
                    "status=%s",
                    session_id,
                    verification.distributor_id,
                    verification.status,
                )

    retry_on_lock_contention(_attempt)


def approve_kyc(distributor) -> None:
    """Task 11c: the only place `Distributor.ir_id`/`kyc_status` transition
    to approved. Never called from anywhere else -- Didit's own result
    (including its "in_review" state) is informational only, per SPEC.md's
    "never auto-approve KYC" boundary; only this explicit admin action
    approves anyone.

    IR ID generation was run through doubt-driven-development 2026-07-13
    (see tasks/todo.md Task 11c): the sequence row is pre-seeded by a data
    migration rather than lazily created, sidestepping a cold-start race
    entirely rather than reasoning about whether a retry wrapper covers
    every shape of it. The idempotency guard checks both `kyc_status` and
    `ir_id` (not just one), since a future flow that resets `kyc_status`
    away from approved without touching `ir_id` would otherwise mint a
    second, ID-losing IR ID for the same distributor. Approving a
    previously-`rejected` distributor is deliberately allowed -- rejection
    isn't final; an admin can approve a fixed resubmission.
    """

    def _attempt():
        with transaction.atomic():
            try:
                locked = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=distributor.pk)
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "approve_kyc: distributor pk=%s no longer exists -- "
                    "cannot approve. Needs manual investigation.",
                    distributor.pk,
                )
                return

            if (
                locked.kyc_status == Distributor.KycStatus.APPROVED
                or locked.ir_id is not None
            ):
                return  # Already approved -- idempotent no-op.

            try:
                sequence = select_for_update_nowait_if_supported(
                    IrIdSequence.objects.filter(pk=1)
                ).get()
            except IrIdSequence.DoesNotExist:
                logger.error(
                    "approve_kyc: IrIdSequence row (pk=1) does not exist -- "
                    "migration 0011_seed_ir_id_sequence should have created "
                    "it. Cannot approve distributor pk=%s. Needs manual "
                    "investigation.",
                    distributor.pk,
                )
                return
            number = sequence.next_number
            max_number = 10**config.IR_ID_NUMBER_OF_DIGITS - 1
            if number > max_number:
                # A genuine operational emergency -- the business cannot
                # onboard another distributor until IR_ID_NUMBER_OF_DIGITS
                # is increased. Logged loudly (not just raised) so it's
                # findable in structured logs, not just a bare traceback.
                logger.critical(
                    "approve_kyc: IR ID sequence exhausted (next_number=%s, "
                    "IR_ID_NUMBER_OF_DIGITS=%s) -- cannot approve "
                    "distributor pk=%s. Increase IR_ID_NUMBER_OF_DIGITS "
                    "immediately.",
                    number,
                    config.IR_ID_NUMBER_OF_DIGITS,
                    distributor.pk,
                )
                raise IrIdSequenceExhausted(
                    f"IR ID sequence exhausted: next_number={number} exceeds "
                    f"what IR_ID_NUMBER_OF_DIGITS={config.IR_ID_NUMBER_OF_DIGITS} "
                    "digits can represent. Increase that setting before "
                    "approving more distributors."
                )
            sequence.next_number = number + 1
            sequence.save(update_fields=["next_number"])

            locked.ir_id = (
                f"{config.IR_ID_PREFIX}"
                f"{str(number).zfill(config.IR_ID_NUMBER_OF_DIGITS)}"
            )
            locked.kyc_status = Distributor.KycStatus.APPROVED
            locked.save(update_fields=["ir_id", "kyc_status"])
            # Significant, financially/legally relevant business event --
            # who approved is captured separately via Distributor.history
            # (django-simple-history + HistoryRequestMiddleware), this log
            # line is for fast log-based searching/alerting.
            logger.info(
                "approve_kyc: distributor pk=%s approved, ir_id=%s",
                locked.pk,
                locked.ir_id,
            )

    retry_on_lock_contention(_attempt)


def reject_kyc(distributor, reason: str) -> None:
    """Task 11c: rejects a pending (or previously-rejected) distributor's
    KYC with a reason, from the admin's own judgment -- Didit's result is
    shown as context only. Refuses to reject an already-`approved`
    distributor: a permanent IR ID, once assigned, isn't something this
    function un-does; a real "revoke approval" flow would be a separate,
    more deliberate feature."""

    def _attempt():
        with transaction.atomic():
            try:
                locked = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=distributor.pk)
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "reject_kyc: distributor pk=%s no longer exists -- "
                    "cannot reject. Needs manual investigation.",
                    distributor.pk,
                )
                return

            if locked.kyc_status == Distributor.KycStatus.APPROVED:
                logger.warning(
                    "reject_kyc: distributor pk=%s is already approved -- "
                    "refusing to reject an approved distributor.",
                    locked.pk,
                )
                return

            locked.kyc_status = Distributor.KycStatus.REJECTED
            locked.kyc_rejection_reason = reason
            locked.save(update_fields=["kyc_status", "kyc_rejection_reason"])
            # Who rejected + the reason text are in Distributor.history
            # (django-simple-history); this log line is just for fast
            # log-based searching/alerting, so it deliberately omits the
            # reason text itself.
            logger.info("reject_kyc: distributor pk=%s rejected", locked.pk)

    retry_on_lock_contention(_attempt)
