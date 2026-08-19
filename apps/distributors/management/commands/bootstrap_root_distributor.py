import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from phonenumber_field.formfields import PhoneNumberField as PhoneNumberFormField

from apps.binary_tree.services import AlreadyPlacedError, BinaryTree
from apps.distributors.models import Distributor, RootDistributor
from apps.distributors.services import (
    IrIdSequenceExhausted,
    MembershipCancelled,
    StarterPackAlreadyConfirmed,
    approve_kyc,
    snapshot_starter_pack_choice,
)
from apps.pv_ledger.services import record_personal_pv, record_purchase_pv
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

User = get_user_model()


class Command(BaseCommand):
    help = (
        "One-time bootstrap for the very first ('root') distributor account -- "
        "the only distributor who can ever have no sponsor, since the public "
        "/distributors/register/ page always requires a valid Sponsor's IR ID "
        "and none exists until this command creates the first one.\n\n"
        "Without --force, at most one root can be created THIS WAY, and that "
        "guarantee is real at the database level (see "
        "apps.distributors.models.RootDistributor, a locked singleton row) -- "
        "two operators running this at the same moment will never both "
        "succeed; the second one cleanly fails instead of racing the first. "
        "--force is a deliberate escape hatch that bypasses this guarantee "
        "on purpose (see its own help text) -- it does not somehow make the "
        "platform enforce a single root everywhere; nothing stops a "
        "sponsor-less row being created some other way (e.g. Django Admin's "
        "default, unrestricted Sponsor field on an existing distributor).\n\n"
        "Client-confirmed design (2026-08-19): this is a REAL, genuinely "
        "earning distributor account, identical in every way to one created "
        "through the normal paid registration + starter pack flow -- it will "
        "receive Direct Referral Bonus from anyone it personally recruits, "
        "and Binary/Matching Bonus from its entire downline, for as long as "
        "the platform runs. No registration fee or starter pack payment is "
        "actually charged -- this command reproduces "
        "apps.distributors.services.consume_paid_starter_pack's real "
        "post-payment side effects one at a time (tree placement, PV credit, "
        "rank, IR ID assignment via apps.distributors.services.approve_kyc) "
        "rather than routing through Paystack, since there is no real "
        "transaction to verify for an administratively-created account. Kept "
        "in sync with that function's own side effects by hand -- if it ever "
        "changes what a confirmed starter pack purchase does, this command "
        "needs the same update."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--phone",
            required=True,
            help="Phone number, e.g. +233241234567 or 0241234567. Validated "
            "and normalized the same way the public registration form does. "
            "Becomes the distributor's permanent login ID.",
        )
        parser.add_argument("--full-name", required=True, dest="full_name")
        parser.add_argument("--email", default="", help="Optional.")
        parser.add_argument("--address", default="", help="Optional.")
        parser.add_argument("--area", default="", help="Optional.")
        parser.add_argument("--landmark", default="", help="Optional.")
        parser.add_argument(
            "--starter-pack",
            dest="starter_pack",
            choices=["A", "B"],
            required=True,
            help="Which starter pack tier sets this account's starting rank "
            "and PV (A=Bronze, B=Silver by default -- see Platform Settings "
            "for the live rates).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Required if a root distributor already exists on this "
            "platform. This command is meant to run exactly once, before "
            "anyone else has registered -- if a root already exists, you "
            "almost certainly don't want to run this again. Using --force "
            "creates an ADDITIONAL, fully separate sponsor-less distributor "
            "-- the root of its own disconnected binary tree, since "
            "BinaryTree.place_distributor has no concept of 'the' tree, "
            "only of individual placements. The original root's marker "
            "(RootDistributor) is left untouched, so it stays the "
            "platform's one recognized root -- this new one is not "
            "automatically linked to anything.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the interactive confirmation prompt (for scripted use "
            "only -- prefer the interactive prompt for a one-off run).",
        )

    def handle(self, *args, **options):
        full_name = options["full_name"]
        email = options["email"]
        starter_pack = options["starter_pack"]
        force = options["force"]

        phone_field = PhoneNumberFormField()
        try:
            # Same validation/normalization the public registration form
            # applies to this exact field -- not a hand-rolled regex.
            parsed_phone = phone_field.clean(options["phone"])
        except ValidationError as exc:
            raise CommandError(f"Invalid --phone: {'; '.join(exc.messages)}") from exc
        phone = str(parsed_phone)

        # Fast, friendly pre-flight check -- NOT the real safety guarantee
        # (that's the locked RootDistributor row inside _attempt() below).
        # This just avoids prompting for a password only to fail a moment
        # later on the obvious case.
        if (
            RootDistributor.objects.filter(distributor__isnull=False).exists()
            and not force
        ):
            raise CommandError(
                "A root distributor already exists on this platform. "
                "bootstrap_root_distributor is meant to create the very "
                "first distributor account, before anyone else has "
                "registered. If you're certain you want to create another "
                "sponsor-less account anyway, re-run with --force."
            )
        if Distributor.objects.filter(phone_number=phone).exists():
            raise CommandError(
                f"A distributor with phone number {phone} already exists."
            )
        if User.objects.filter(username=phone).exists():
            raise CommandError(f"A user with username {phone} already exists.")

        password = getpass.getpass("Set a password for this account: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise CommandError("Passwords did not match.")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(
                f"Password does not meet requirements: {'; '.join(exc.messages)}"
            ) from exc

        if not options["yes"]:
            summary = (
                f"\nAbout to create a REAL, permanently commission-earning "
                f"root distributor:\n"
                f"  Phone (login ID): {phone}\n"
                f"  Full name:        {full_name}\n"
                f"  Email:            {email or '(none)'}\n"
                f"  Starter pack:     {starter_pack}\n"
            )
            if force:
                existing = RootDistributor.objects.filter(
                    pk=1, distributor__isnull=False
                ).select_related("distributor")
                if existing:
                    root = existing[0].distributor
                    summary += (
                        f"\n--force is set. This platform's existing root is "
                        f"{root.full_name!r} (IR ID {root.ir_id}, phone "
                        f"{root.phone_number}) -- it will be left untouched. "
                        "This new account will be a SEPARATE, disconnected "
                        "root with its own binary tree.\n"
                    )
            self.stdout.write(summary)
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                raise CommandError("Aborted -- nothing was created.")

        def _attempt():
            with transaction.atomic():
                # The real guarantee: locks the one singleton row, matching
                # this codebase's own established convention
                # (bancostore/concurrency.py) for exactly this kind of
                # counter/singleton row. On MySQL/Postgres (NOWAIT
                # supported), a concurrent invocation holding this same lock
                # fails immediately with a lock-contention OperationalError
                # -- retry_on_lock_contention (wrapping this whole function,
                # below) retries that a bounded number of times with
                # backoff, then the retried call re-reads root_marker fresh
                # and correctly sees whatever the other invocation actually
                # committed. Never a blind race with the pre-flight check
                # above. See RootDistributor's own docstring for why this
                # is a locked row rather than a partial unique constraint
                # (MySQL, this project's production database, has no
                # partial-index support).
                root_marker = select_for_update_nowait_if_supported(
                    RootDistributor.objects.filter(pk=1)
                ).get()
                if root_marker.distributor_id is not None and not force:
                    raise CommandError(
                        f"A root distributor already exists (id="
                        f"{root_marker.distributor_id}). Re-run with --force "
                        "if you're certain you want to create another "
                        "sponsor-less account anyway -- the original root "
                        "marker will be left untouched."
                    )

                # Trade-off, deliberately not restructured: everything below
                # this point runs inside the RootDistributor lock acquired
                # above, and snapshot_starter_pack_choice/approve_kyc each
                # open their OWN retry_on_lock_contention block internally
                # -- nested retries, which apps/pv_ledger/services.py::
                # record_personal_pv's own docstring explicitly calls out
                # as a pattern this codebase normally avoids elsewhere
                # (retrying twice can amplify backoff and extend how long
                # an outer lock is held). Accepted here rather than
                # restructured: this command runs exactly once, interactively,
                # for a few seconds, before the platform has real
                # traffic -- the realistic risk is a few hundred ms of
                # extra RootDistributor lock hold time if it happens to
                # contend with something else at that exact moment, not a
                # correctness problem (proven by
                # test_two_simultaneous_bootstraps_never_both_succeed).
                user = User(username=phone, email=email)
                user.set_password(password)
                user.full_clean()
                user.save()

                distributor_group, _ = Group.objects.get_or_create(name="distributor")
                user.groups.add(distributor_group)

                distributor = Distributor(
                    user=user,
                    phone_number=phone,
                    full_name=full_name,
                    address=options["address"],
                    area=options["area"],
                    landmark=options["landmark"],
                    sponsor=None,
                    phone_verified=True,
                )
                distributor.full_clean()
                distributor.save()

                distributor = snapshot_starter_pack_choice(distributor.pk, starter_pack)

                try:
                    # The one already-shipped, already-correct "root" case
                    # in BinaryTree.place_distributor: sponsor=None places
                    # this distributor as the tree's own root, no ancestors
                    # to walk. Confirmed by reading that function directly,
                    # not assumed.
                    BinaryTree.place_distributor(None, distributor, leg=None)
                except AlreadyPlacedError as exc:
                    # Deliberately fatal here, unlike consume_paid_starter_
                    # pack's own graceful degrade for this same exception --
                    # that function treats it as a webhook-replay quirk on
                    # an EXISTING distributor; for a distributor this
                    # command just created in this same transaction,
                    # already being tree-placed is impossible and would
                    # indicate a real bug, not a benign replay.
                    raise CommandError(
                        "Unexpected: this brand-new distributor is already "
                        "placed in the binary tree."
                    ) from exc

                now = timezone.now()
                today = now.date()
                # Mirrors consume_paid_starter_pack exactly: record_purchase_pv
                # credits ancestors' legs (a no-op here -- this distributor
                # has none), record_personal_pv credits this distributor's
                # OWN monthly PV, needed for Binary/Matching Bonus
                # eligibility. Two distinct ledgers, not a double-credit of
                # the same thing -- confirmed by reading both functions.
                record_purchase_pv(
                    distributor, distributor.starter_pack_pv, today=today
                )
                record_personal_pv(
                    distributor, distributor.starter_pack_pv, today=today
                )

                distributor.rank = distributor.starter_pack_rank
                distributor.starter_pack_confirmed_at = now
                distributor.save(update_fields=["rank", "starter_pack_confirmed_at"])

                approve_kyc(distributor)
                distributor.refresh_from_db()

                # approve_kyc has no return value a caller can check -- it
                # silently no-ops (only logging an error) if, for example,
                # the IrIdSequence seed row is missing. Verify the real
                # outcome before this transaction is allowed to commit and
                # before anything is reported as successful.
                if (
                    distributor.kyc_status != Distributor.KycStatus.APPROVED
                    or not distributor.ir_id
                ):
                    raise CommandError(
                        "approve_kyc did not result in an approved, IR-ID-"
                        "assigned distributor (kyc_status="
                        f"{distributor.kyc_status!r}, ir_id="
                        f"{distributor.ir_id!r}). This usually means the "
                        "IrIdSequence seed row is missing -- check that "
                        "migration 0011_seed_ir_id_sequence has been "
                        "applied. Rolling back; nothing was created."
                    )

                # Only the very first successful bootstrap ever claims the
                # singleton marker -- a --force-created second sponsor-less
                # distributor deliberately leaves the original root's
                # marker untouched, so RootDistributor always points at the
                # platform's true, original root, never whichever one ran
                # most recently.
                if root_marker.distributor_id is None:
                    root_marker.distributor = distributor
                    root_marker.save(update_fields=["distributor"])

                return distributor

        try:
            distributor = retry_on_lock_contention(_attempt)
        except (
            ValidationError,
            IntegrityError,
            IrIdSequenceExhausted,
            StarterPackAlreadyConfirmed,
            MembershipCancelled,
        ) as exc:
            raise CommandError(f"Could not create the account: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                "\nRoot distributor created successfully.\n"
                f"  Login phone number: {phone}\n"
                f"  Full name:          {full_name}\n"
                f"  IR ID:               {distributor.ir_id}\n"
                f"  Rank:                {distributor.rank}\n"
                f"  KYC status:          {distributor.kyc_status}\n\n"
                "This account can log in immediately at "
                "https://bancostore.com/distributors/login/ using the phone "
                "number and password just set (no OTP/KYC wait, since both "
                "were bypassed administratively for this one bootstrap "
                "account). Its IR ID above is the Sponsor's IR ID that the "
                "very first real recruit should enter on the public "
                "registration page.\n\n"
                "This is a real, genuinely earning distributor account -- it "
                "will receive commissions from its own downline exactly "
                "like any other distributor, per the client-confirmed "
                "design decision."
            )
        )
