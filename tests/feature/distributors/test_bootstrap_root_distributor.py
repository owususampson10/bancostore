import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection

import pytest
from constance import config

from apps.commissions.services import calculate_direct_referral_bonus
from apps.distributors.management.commands import (
    bootstrap_root_distributor as cmd_module,
)
from apps.distributors.models import Distributor, IrIdSequence, RootDistributor
from apps.distributors.services import consume_paid_starter_pack
from apps.pv_ledger.models import MonthlyPersonalPv
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()

STRONG_PASSWORD = "Correct-Horse-Battery-Staple-42"


@pytest.fixture(autouse=True)
def _ensure_seeded_singleton_rows(db):
    """Same known issue, same established fix as
    tests/unit/distributors/test_kyc_review.py::_ensure_ir_id_sequence_row:
    both apps/distributors/migrations/0011_seed_ir_id_sequence.py and
    0017_seed_root_distributor_singleton.py seed their pk=1 row ONCE, at
    initial test-database setup -- but any transaction=True test anywhere
    in the suite flushes the database afterward (standard Django
    TransactionTestCase behavior), and flush does NOT re-run data
    migrations. Found via a real combined-file run:
    test_two_simultaneous_bootstraps_never_both_succeed (this file's own
    transaction=True test) passed reliably in isolation but failed twice
    in a row when run after test_kyc_review.py's own transaction=True
    test flushed the database -- first with RootDistributor.DoesNotExist,
    and then, once that was fixed here, with IrIdSequence.DoesNotExist
    (bootstrap_root_distributor's own approve_kyc() call needs that row
    too, exactly like test_kyc_review.py's own tests do -- restoring only
    RootDistributor here wasn't the full fix)."""
    RootDistributor.objects.get_or_create(pk=1, defaults={"distributor": None})
    IrIdSequence.objects.get_or_create(pk=1, defaults={"next_number": 1})


def _bootstrap(
    *,
    phone="+233241000001",
    full_name="Founder Distributor",
    starter_pack="A",
    force=False,
    password_side_effect=None,
):
    args = [
        "--phone",
        phone,
        "--full-name",
        full_name,
        "--starter-pack",
        starter_pack,
        "--yes",
    ]
    if force:
        args.append("--force")

    with patch(
        "getpass.getpass",
        side_effect=password_side_effect or [STRONG_PASSWORD, STRONG_PASSWORD],
    ):
        call_command("bootstrap_root_distributor", *args)


@pytest.mark.django_db
def test_creates_an_approved_sponsor_less_distributor_with_a_real_ir_id():
    _bootstrap()

    distributor = Distributor.objects.get(phone_number="+233241000001")
    assert distributor.sponsor is None
    assert distributor.phone_verified is True
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id
    assert distributor.ir_id.startswith(config.IR_ID_PREFIX)
    assert distributor.rank == config.STARTER_PACK_A_RANK
    assert distributor.starter_pack_pv == config.STARTER_PACK_A_PV
    assert distributor.starter_pack_confirmed_at is not None
    assert distributor.user.groups.filter(name="distributor").exists()
    assert distributor.user.check_password(STRONG_PASSWORD)


@pytest.mark.django_db
def test_credits_the_root_distributors_own_monthly_personal_pv():
    _bootstrap(starter_pack="B")

    distributor = Distributor.objects.get(phone_number="+233241000001")
    assert MonthlyPersonalPv.objects.filter(
        distributor=distributor, pv=config.STARTER_PACK_B_PV
    ).exists()


@pytest.mark.django_db
def test_refuses_to_run_a_second_time_without_force():
    _bootstrap(phone="+233241000001")

    with pytest.raises(CommandError, match="already exists"):
        _bootstrap(phone="+233241000002")

    assert Distributor.objects.count() == 1


@pytest.mark.django_db
def test_force_allows_a_second_sponsor_less_distributor_deliberately():
    _bootstrap(phone="+233241000001")

    _bootstrap(phone="+233241000002", force=True)

    assert Distributor.objects.filter(sponsor__isnull=True).count() == 2


@pytest.mark.django_db
def test_force_confirmation_prompt_shows_the_existing_roots_identity():
    """The interactive --force confirmation must name the real, existing
    root before an operator can accidentally create a disconnected
    second one without realizing there already is one."""
    import io

    _bootstrap(phone="+233241000001", full_name="Original Founder")
    original_root = Distributor.objects.get(phone_number="+233241000001")

    out = io.StringIO()
    with patch("getpass.getpass", return_value=STRONG_PASSWORD):
        with patch("builtins.input", return_value="yes"):
            call_command(
                "bootstrap_root_distributor",
                "--phone",
                "+233241000002",
                "--full-name",
                "Second Founder",
                "--starter-pack",
                "A",
                "--force",
                stdout=out,
            )

    output = out.getvalue()
    assert "Original Founder" in output
    assert original_root.ir_id in output
    assert Distributor.objects.filter(sponsor__isnull=True).count() == 2


@pytest.mark.django_db
def test_refuses_a_duplicate_phone_number():
    _bootstrap(phone="+233241000001")

    with pytest.raises(CommandError, match="already exists"):
        _bootstrap(phone="+233241000001", force=True)

    assert Distributor.objects.count() == 1


@pytest.mark.django_db
def test_mismatched_passwords_create_nothing():
    with pytest.raises(CommandError, match="did not match"):
        _bootstrap(password_side_effect=[STRONG_PASSWORD, "something-else-entirely"])

    assert Distributor.objects.count() == 0
    assert User.objects.count() == 0


@pytest.mark.django_db
def test_weak_password_is_rejected_and_creates_nothing():
    with pytest.raises(CommandError, match="requirements"):
        _bootstrap(password_side_effect=["1234", "1234"])

    assert Distributor.objects.count() == 0
    assert User.objects.count() == 0


@pytest.mark.django_db
def test_invalid_phone_number_is_rejected_before_anything_is_created():
    with pytest.raises(CommandError, match="Invalid --phone"):
        _bootstrap(phone="not-a-real-phone-number")

    assert Distributor.objects.count() == 0
    assert User.objects.count() == 0


@pytest.mark.django_db
def test_root_distributor_earns_a_real_direct_referral_bonus_from_a_real_recruit():
    """The core contract this command exists to satisfy: once a genuine
    distributor registers under the bootstrapped root (through the exact
    same production code path as any other distributor -- a locked,
    idempotent starter-pack confirmation), the root must be paid a real
    Direct Referral Bonus exactly like any other sponsor would be. This
    exercises the real apps.distributors.services.consume_paid_starter_pack
    end to end, not a mock of it."""
    _bootstrap(phone="+233241000001", starter_pack="A")
    root = Distributor.objects.get(phone_number="+233241000001")
    assert not Wallet.objects.filter(distributor=root).exists()

    recruit_phone = "+233241000099"
    recruit_user = User.objects.create_user(
        username=recruit_phone, password="Passw0rd!"
    )
    recruit = Distributor.objects.create(
        user=recruit_user, phone_number=recruit_phone, sponsor=root
    )
    recruit.starter_pack_choice = "B"
    recruit.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    recruit.starter_pack_pv = config.STARTER_PACK_B_PV
    recruit.starter_pack_rank = config.STARTER_PACK_B_RANK
    recruit.starter_pack_payment_reference = "recruit-pack-ref"
    recruit.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )

    with patch("apps.distributors.services.verify_transaction") as mock_verify:
        mock_verify.return_value = {
            "status": "success",
            "amount": recruit.starter_pack_price_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack("recruit-pack-ref")

    recruit.refresh_from_db()
    assert recruit.rank == config.STARTER_PACK_B_RANK

    root_wallet = Wallet.objects.get(distributor=root)
    expected_bonus = calculate_direct_referral_bonus(config.STARTER_PACK_B_PV)
    assert root_wallet.balance == expected_bonus
    assert WalletTransaction.objects.filter(
        wallet=root_wallet,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        amount=expected_bonus,
    ).exists()


@pytest.mark.django_db
def test_the_singleton_marker_points_at_the_real_root_after_success():
    _bootstrap(phone="+233241000001")

    distributor = Distributor.objects.get(phone_number="+233241000001")
    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id == distributor.pk


@pytest.mark.django_db
def test_force_creating_a_second_root_never_overwrites_the_original_marker():
    _bootstrap(phone="+233241000001")
    original_root = Distributor.objects.get(phone_number="+233241000001")

    _bootstrap(phone="+233241000002", force=True)

    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id == original_root.pk


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_bootstraps_never_both_succeed():
    """The core guarantee this database-level change exists to provide:
    two operators (or two copies of a deploy script) running this command
    at the exact same moment must never both create a sponsor-less root --
    exactly one must win, the other must cleanly fail rather than racing
    it. Uses real separate threads/connections, matching this codebase's
    own established convention for proving a select_for_update() lock
    actually serializes concurrent writers (see
    tests/unit/distributors/test_consume_paid_starter_pack.py's own
    threading tests) -- @pytest.mark.django_db(transaction=True) is
    required for this, since the default transactional-test-wrapper
    fixture would otherwise hide every thread's writes from every other
    thread. The getpass patch is applied ONCE, wrapping both threads,
    rather than inside each thread's own attempt() -- unittest.mock.patch
    is not thread-safe for concurrent enter/exit on the same target (a
    CodeRabbit finding on the original version of this test): two threads
    each entering/exiting their own `with patch(...)` block on the same
    global attribute can save and restore each other's mock instead of
    the real function, either leaking a mock into later tests or leaving
    getpass permanently patched.

    A later CodeRabbit finding caught that simply starting two threads
    does not guarantee they actually overlap at the lock -- the scheduler
    could run them fully sequentially, in which case this test would pass
    for a reason that proves nothing about real concurrent safety (the
    second call would see the first's already-committed row via the
    ordinary pre-flight check, never touching contention at all). Fixed
    by intercepting bootstrap_root_distributor's own call to
    select_for_update_nowait_if_supported (patched only in that module's
    namespace, so the internal locks snapshot_starter_pack_choice/
    approve_kyc take on other rows are untouched): the first thread to
    reach it is forced to pause -- genuine DB lock still held, its
    transaction still open -- until the second thread has actually
    reached the same point.

    A further CodeRabbit finding caught that this first fix was still
    incomplete: the test body released the first thread as soon as ITS
    OWN lock was acquired, with no confirmation the second thread had
    even started its own .get() call yet -- so the first thread could
    still finish and commit before the second ever attempted real
    contention. Fixed with a second signal (second_reached_lock),
    fired the instant the second thread's own contended query begins,
    that the test body also waits for before releasing the first."""
    results = {}
    first_reached_lock = threading.Event()
    second_reached_lock = threading.Event()
    release_first = threading.Event()
    claim_lock = threading.Lock()
    call_order = {"n": 0}

    real_lock_helper = cmd_module.select_for_update_nowait_if_supported

    def synchronizing_lock_helper(queryset):
        with claim_lock:
            call_order["n"] += 1
            is_first_caller = call_order["n"] == 1
        locked_queryset = real_lock_helper(queryset)
        real_get = locked_queryset.get
        if is_first_caller:

            def paused_get(*args, **kwargs):
                # The real query runs here -- the DB lock is genuinely
                # acquired, and this thread's transaction stays open
                # (nothing has returned control back to the `with
                # transaction.atomic():` block yet) for as long as this
                # stays paused below. Waits for second_reached_lock too,
                # not just its own timeout -- a CodeRabbit finding on the
                # first version of this synchronization caught that
                # releasing the first thread as soon as ITS OWN lock was
                # acquired, with no confirmation the second thread had
                # even started its own .get() yet, could still let the
                # first thread finish and commit before the second ever
                # attempted real contention.
                row = real_get(*args, **kwargs)
                first_reached_lock.set()
                release_first.wait(timeout=5)
                return row

            locked_queryset.get = paused_get
        else:
            # Only proceed once the first caller has genuinely acquired
            # its lock and is holding it open -- this is what guarantees
            # real overlap instead of a scheduling accident.
            first_reached_lock.wait(timeout=5)

            def signaling_get(*args, **kwargs):
                # Signals the instant this thread actually starts its
                # own contended query -- not merely that it reached this
                # wrapper -- so the test body knows real overlap has
                # begun before it lets the first thread go.
                second_reached_lock.set()
                return real_get(*args, **kwargs)

            locked_queryset.get = signaling_get
        return locked_queryset

    def attempt(key, phone):
        try:
            call_command(
                "bootstrap_root_distributor",
                "--phone",
                phone,
                "--full-name",
                "Founder",
                "--starter-pack",
                "A",
                "--yes",
            )
            results[key] = "success"
        except CommandError as exc:
            results[key] = f"error: {exc}"
        finally:
            connection.close()  # each thread must not share the main
            # thread's connection/transaction state

    with (
        patch("getpass.getpass", return_value=STRONG_PASSWORD),
        patch.object(
            cmd_module,
            "select_for_update_nowait_if_supported",
            side_effect=synchronizing_lock_helper,
        ),
    ):
        threads = [
            threading.Thread(target=attempt, args=("first", "+233241000001")),
            threading.Thread(target=attempt, args=("second", "+233241000002")),
        ]
        for t in threads:
            t.start()
        # Wait for BOTH signals, not just the first thread's own lock
        # acquisition: the second thread must have genuinely started its
        # own contended query too, or the first thread could be released
        # and finish before the second ever attempts real overlap.
        first_reached_lock.wait(timeout=5)
        second_reached_lock.wait(timeout=5)
        release_first.set()
        for t in threads:
            t.join()

    outcomes = list(results.values())
    assert outcomes.count("success") == 1, results
    errors = [o for o in outcomes if o.startswith("error")]
    assert len(errors) == 1, results
    # Proves the loser failed on the singleton lock specifically, not on
    # some unrelated guard (e.g. a duplicate-phone or missing-migration
    # error) that would happen to also satisfy a bare "was there an error"
    # check.
    assert "already exists" in errors[0], results
    assert Distributor.objects.filter(sponsor__isnull=True).count() == 1
    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id is not None
