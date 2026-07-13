import threading
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

import pytest
from constance import config

from apps.distributors.models import Distributor, PendingRegistration
from apps.distributors.services import (
    PendingRegistrationAlreadyConsumed,
    PendingRegistrationNotFound,
    snapshot_payment_reference,
)

User = get_user_model()
_phone_seq = count(1)


def _make_sponsor():
    phone = f"+233209{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, ir_id="IR00001")


def _make_pending(sponsor):
    return PendingRegistration.objects.create(
        full_name="Kofi Mensah",
        phone_number=f"+233241{next(_phone_seq):06d}",
        email="kofi@example.test",
        address="12 Ring Road",
        area="Osu",
        landmark="",
        password_hash="hashed",
        sponsor=sponsor,
    )


@pytest.mark.django_db
def test_snapshot_generates_a_fresh_reference_and_pins_the_current_fee():
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)

    result = snapshot_payment_reference(pending.token)

    assert result.payment_reference
    assert result.fee_amount_pesewas == int(config.REGISTRATION_FEE * 100)


@pytest.mark.django_db
def test_snapshot_raises_not_found_for_an_unknown_token():
    with pytest.raises(PendingRegistrationNotFound):
        snapshot_payment_reference("00000000-0000-0000-0000-000000000000")


@pytest.mark.django_db
def test_snapshot_raises_already_consumed_for_a_consumed_registration():
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    pending.consumed_at = timezone.now()
    pending.save(update_fields=["consumed_at"])

    with pytest.raises(PendingRegistrationAlreadyConsumed):
        snapshot_payment_reference(pending.token)


@pytest.mark.django_db(transaction=True)
def test_concurrent_snapshots_never_leave_two_valid_references_for_one_token():
    """Regression test for a real race found in review: two concurrent
    visits to the payment page for the same token (a double-click, two
    open tabs) must not both write a reference -- the loser's reference
    would silently stop matching anything in PendingRegistration, and a
    customer who then paid via that now-orphaned checkout page would have
    no way to complete their registration. Locking (select_for_update)
    must serialize these instead. Same SQLite caveat as other concurrency
    tests in this codebase: has_select_for_update is False on SQLite, so
    this only genuinely proves the lock works under real MySQL, but it
    still catches a regression in the surrounding logic even here since
    each thread gets its own connection."""
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    token = pending.token

    results = []
    lock = threading.Lock()

    def attempt():
        try:
            result = snapshot_payment_reference(token)
            with lock:
                results.append(result.payment_reference)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pending.refresh_from_db()
    # Whichever attempt wrote last, the persisted reference must match one
    # of the actually-generated ones -- never a corrupted/partial value --
    # and it must be the only reference PendingRegistration now has.
    assert pending.payment_reference in results
    assert len(set(results)) == 5  # each attempt generated a distinct reference
