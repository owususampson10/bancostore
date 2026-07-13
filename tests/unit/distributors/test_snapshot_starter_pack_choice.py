import threading
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.distributors.services import (
    StarterPackAlreadyConfirmed,
    snapshot_starter_pack_choice,
)

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233241{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_pack_b_pins_the_current_price_pv_and_rank():
    distributor = _make_distributor()

    result = snapshot_starter_pack_choice(distributor.pk, "B")

    assert result.starter_pack_choice == "B"
    assert result.starter_pack_price_pesewas == int(config.STARTER_PACK_B_PRICE * 100)
    assert result.starter_pack_pv == config.STARTER_PACK_B_PV
    assert result.starter_pack_rank == config.STARTER_PACK_B_RANK
    assert result.starter_pack_payment_reference


@pytest.mark.django_db
def test_pack_a_pins_the_current_price_pv_and_rank():
    distributor = _make_distributor()

    result = snapshot_starter_pack_choice(distributor.pk, "A")

    assert result.starter_pack_choice == "A"
    assert result.starter_pack_price_pesewas == int(config.STARTER_PACK_A_PRICE * 100)
    assert result.starter_pack_pv == config.STARTER_PACK_A_PV
    assert result.starter_pack_rank == config.STARTER_PACK_A_RANK


@pytest.mark.django_db
def test_raises_if_already_confirmed():
    distributor = _make_distributor()
    distributor.starter_pack_confirmed_at = timezone.now()
    distributor.save(update_fields=["starter_pack_confirmed_at"])

    with pytest.raises(StarterPackAlreadyConfirmed):
        snapshot_starter_pack_choice(distributor.pk, "B")


@pytest.mark.django_db(transaction=True)
def test_concurrent_snapshots_never_leave_two_valid_references_for_one_distributor():
    """Same race class as snapshot_payment_reference: two concurrent visits
    to the pack-selection page for the same distributor (double-click, two
    tabs) must not overwrite each other's reference. Same SQLite caveat as
    other concurrency tests in this codebase (has_select_for_update is
    False on SQLite), but this still catches a regression in the
    surrounding logic even here."""
    distributor = _make_distributor()

    results = []
    lock = threading.Lock()

    def attempt():
        try:
            result = snapshot_starter_pack_choice(distributor.pk, "B")
            with lock:
                results.append(result.starter_pack_payment_reference)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    distributor.refresh_from_db()
    assert distributor.starter_pack_payment_reference in results
    assert len(set(results)) == 5
