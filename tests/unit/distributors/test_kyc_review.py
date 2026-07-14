import threading
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection

import pytest
from constance import config

from apps.distributors.models import Distributor, IrIdSequence
from apps.distributors.services import IrIdSequenceExhausted, approve_kyc, reject_kyc

User = get_user_model()
_phone_seq = count(1)


@pytest.fixture(autouse=True)
def _ensure_ir_id_sequence_row(db):
    """apps/distributors/migrations/0011_seed_ir_id_sequence.py seeds this
    row ONCE, at initial test-database setup -- but any transaction=True
    test elsewhere in the suite flushes the database afterward (standard
    Django TransactionTestCase behavior), and flush does NOT re-run data
    migrations. Found via a real full-suite failure: the threaded test
    below passed in isolation but failed with IrIdSequence.DoesNotExist
    when run after test_consume_paid_starter_pack.py's own
    transaction=True test flushed it away first. Module-scoped (not in
    conftest.py) so it doesn't force database access onto every test in
    the suite -- only this file and test_kyc_admin.py need it."""
    IrIdSequence.objects.get_or_create(pk=1, defaults={"next_number": 1})


def _make_distributor(kyc_status=Distributor.KycStatus.PENDING):
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, kyc_status=kyc_status
    )


@pytest.mark.django_db
def test_approve_sets_approved_status_and_assigns_ir_id():
    distributor = _make_distributor()

    approve_kyc(distributor)

    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id == "IR00001"


@pytest.mark.django_db
def test_first_ever_ir_id_matches_configured_prefix_and_starting_number():
    distributor = _make_distributor()

    approve_kyc(distributor)

    distributor.refresh_from_db()
    padded_number = str(config.IR_ID_STARTING_NUMBER).zfill(
        config.IR_ID_NUMBER_OF_DIGITS
    )
    expected = f"{config.IR_ID_PREFIX}{padded_number}"
    assert distributor.ir_id == expected


@pytest.mark.django_db
def test_sequential_approvals_get_sequential_ir_ids_with_no_gaps():
    first = _make_distributor()
    second = _make_distributor()
    third = _make_distributor()

    approve_kyc(first)
    approve_kyc(second)
    approve_kyc(third)

    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    assert [first.ir_id, second.ir_id, third.ir_id] == ["IR00001", "IR00002", "IR00003"]


@pytest.mark.django_db
def test_approving_an_already_approved_distributor_is_a_safe_no_op():
    distributor = _make_distributor()
    approve_kyc(distributor)
    distributor.refresh_from_db()
    first_ir_id = distributor.ir_id
    sequence_before = IrIdSequence.objects.get(pk=1).next_number

    approve_kyc(distributor)

    distributor.refresh_from_db()
    assert distributor.ir_id == first_ir_id
    assert IrIdSequence.objects.get(pk=1).next_number == sequence_before


@pytest.mark.django_db
def test_approving_a_previously_rejected_distributor_is_allowed():
    """Deliberate design choice: an admin can override a prior rejection
    (e.g. a blurry resubmission gets fixed) -- rejection is not final."""
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.REJECTED)

    approve_kyc(distributor)

    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id == "IR00001"


@pytest.mark.django_db
def test_ir_id_is_never_assigned_before_approval():
    distributor = _make_distributor()

    assert distributor.ir_id is None


@pytest.mark.django_db
def test_reject_sets_rejected_status_and_reason_without_assigning_ir_id():
    distributor = _make_distributor()

    reject_kyc(distributor, reason="Photo too blurry to read")

    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.REJECTED
    assert distributor.kyc_rejection_reason == "Photo too blurry to read"
    assert distributor.ir_id is None


@pytest.mark.django_db
def test_reject_does_not_advance_the_ir_id_sequence():
    distributor = _make_distributor()
    sequence_before = IrIdSequence.objects.get(pk=1).next_number

    reject_kyc(distributor, reason="Selfie doesn't match ID photo")

    assert IrIdSequence.objects.get(pk=1).next_number == sequence_before


@pytest.mark.django_db
def test_rejecting_an_already_approved_distributor_is_refused():
    """A permanent IR ID, once assigned, isn't undone by a simple reject
    click -- a real revoke-approval flow would be a separate feature."""
    distributor = _make_distributor()
    approve_kyc(distributor)
    distributor.refresh_from_db()
    assigned_ir_id = distributor.ir_id

    reject_kyc(distributor, reason="Changed my mind")

    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id == assigned_ir_id
    assert distributor.kyc_rejection_reason == ""


@pytest.mark.django_db
def test_ir_id_sequence_exhaustion_raises_instead_of_producing_a_malformed_id():
    IrIdSequence.objects.filter(pk=1).update(
        next_number=10**config.IR_ID_NUMBER_OF_DIGITS
    )
    distributor = _make_distributor()

    with pytest.raises(IrIdSequenceExhausted):
        approve_kyc(distributor)

    distributor.refresh_from_db()
    assert distributor.ir_id is None
    assert distributor.kyc_status == Distributor.KycStatus.PENDING


@pytest.mark.django_db
def test_approving_when_the_ir_id_sequence_row_is_missing_does_not_crash():
    """Found via a real full-suite failure (see the module-level
    _ensure_ir_id_sequence_row fixture docstring): the sequence row can go
    missing if a transaction=True test elsewhere flushed it away. This
    proves approve_kyc handles that gracefully rather than propagating an
    unhandled DoesNotExist -- the same "log + return" pattern already used
    for a missing Distributor above."""
    IrIdSequence.objects.filter(pk=1).delete()
    distributor = _make_distributor()

    approve_kyc(distributor)  # must not raise

    distributor.refresh_from_db()
    assert distributor.ir_id is None
    assert distributor.kyc_status == Distributor.KycStatus.PENDING


@pytest.mark.django_db
def test_approving_a_deleted_distributor_does_not_crash():
    distributor = _make_distributor()
    pk = distributor.pk
    Distributor.objects.filter(pk=pk).delete()
    distributor.pk = pk  # stale in-memory reference, as if a stale admin page

    approve_kyc(distributor)  # must not raise


@pytest.mark.django_db(transaction=True)
def test_concurrent_approvals_of_different_distributors_never_duplicate_ir_ids():
    """Same convention as tests/unit/binary_tree/test_placement.py::
    test_concurrent_placements_under_the_same_sponsor: SQLite doesn't
    support real row locking (has_select_for_update_nowait is False), so
    this only genuinely proves exclusion once run against real MySQL in
    CI -- but it still catches a regression in the surrounding lock/retry
    wiring even on SQLite, since each thread gets its own connection."""
    distributors = [_make_distributor() for _ in range(8)]

    def attempt(distributor):
        try:
            approve_kyc(distributor)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(d,)) for d in distributors]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ir_ids = list(
        Distributor.objects.filter(pk__in=[d.pk for d in distributors]).values_list(
            "ir_id", flat=True
        )
    )
    assert len(ir_ids) == len(set(ir_ids))  # no duplicates
    assert all(ir_id is not None for ir_id in ir_ids)  # every one got assigned
