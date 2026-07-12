from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command

import pytest

from apps.distributors.models import Distributor

User = get_user_model()


@pytest.mark.django_db
def test_seed_roles_creates_three_groups(settings):
    settings.DEBUG = True
    call_command("seed_roles")

    assert set(Group.objects.values_list("name", flat=True)) >= {
        "customer",
        "distributor",
        "admin",
    }


@pytest.mark.django_db
def test_seed_roles_creates_one_stub_user_per_role(settings):
    settings.DEBUG = True
    call_command("seed_roles")

    assert User.objects.filter(groups__name="customer").exists()
    assert User.objects.filter(groups__name="distributor").exists()
    assert User.objects.filter(groups__name="admin").exists()


@pytest.mark.django_db
def test_seed_roles_distributor_stub_has_distributor_row(settings):
    settings.DEBUG = True
    call_command("seed_roles")

    distributor_user = User.objects.get(groups__name="distributor")
    assert Distributor.objects.filter(user=distributor_user).exists()


@pytest.mark.django_db
def test_seed_roles_is_idempotent(settings):
    settings.DEBUG = True
    call_command("seed_roles")
    call_command("seed_roles")

    assert User.objects.filter(groups__name="distributor").count() == 1


@pytest.mark.django_db
def test_seed_roles_refuses_to_run_outside_debug(settings):
    """seed_roles creates a hardcoded-password, is_staff=True account
    (STUB_PASSWORD, apps/accounts/management/commands/seed_roles.py) — a
    real risk if this ever ran against a production database (e.g. a
    deploy script that runs "migrate && seed_roles" for convenience)."""
    settings.DEBUG = False

    with pytest.raises(CommandError):
        call_command("seed_roles")

    assert not User.objects.filter(username="stub_admin").exists()
