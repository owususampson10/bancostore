import importlib
from itertools import count

from django.apps import apps as live_apps
from django.contrib.auth import get_user_model

import pytest

from apps.distributors.models import Distributor, RootDistributor

User = get_user_model()
_phone_seq = count(1)

# Migration module names start with a digit, so they can't be imported with
# a normal `from ... import ...` statement -- importlib is the standard way
# to reach one directly for a unit test.
_seed_migration = importlib.import_module(
    "apps.distributors.migrations.0017_seed_root_distributor_singleton"
)


def _make_distributor(sponsor=None):
    phone = f"+233241{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


@pytest.mark.django_db
def test_points_the_marker_at_an_already_existing_sponsor_less_distributor():
    """The real bug a fresh-context adversarial review caught: a blind
    `defaults={"distributor": None}` seed would leave the marker unaware
    of a sponsor-less distributor that already existed before this
    feature shipped (e.g. hand-created, or edited via Django Admin's
    unrestricted `sponsor` field) -- bootstrap_root_distributor only ever
    trusts this marker, never a live query, so that gap could let it
    create a second "official" root undetected."""
    RootDistributor.objects.filter(pk=1).delete()
    existing_root = _make_distributor(sponsor=None)

    _seed_migration.seed_root_distributor_singleton(live_apps, schema_editor=None)

    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id == existing_root.pk


@pytest.mark.django_db
def test_seeds_a_blank_marker_when_no_sponsor_less_distributor_exists_yet():
    RootDistributor.objects.filter(pk=1).delete()

    _seed_migration.seed_root_distributor_singleton(live_apps, schema_editor=None)

    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id is None


@pytest.mark.django_db
def test_picks_the_oldest_when_multiple_sponsor_less_distributors_exist(capsys):
    RootDistributor.objects.filter(pk=1).delete()
    older = _make_distributor(sponsor=None)
    _make_distributor(sponsor=None)

    _seed_migration.seed_root_distributor_singleton(live_apps, schema_editor=None)

    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id == older.pk
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.django_db
def test_is_idempotent_when_the_marker_row_already_exists():
    # get_or_create rather than a plain .get(): a transaction=True test
    # elsewhere in the suite may have already flushed the migration-seeded
    # row away (same known issue as tests/unit/distributors/
    # test_kyc_review.py::_ensure_ir_id_sequence_row) -- recreate it here
    # so this test's own precondition ("the row already exists") holds
    # regardless of what ran before it.
    original, _ = RootDistributor.objects.get_or_create(
        pk=1, defaults={"distributor": None}
    )
    _make_distributor(sponsor=None)  # created AFTER the marker already exists

    _seed_migration.seed_root_distributor_singleton(live_apps, schema_editor=None)

    marker = RootDistributor.objects.get(pk=1)
    assert marker.distributor_id == original.distributor_id
