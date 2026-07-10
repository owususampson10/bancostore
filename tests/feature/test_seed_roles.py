from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

import pytest

from apps.distributors.models import Distributor

User = get_user_model()


@pytest.mark.django_db
def test_seed_roles_creates_three_groups():
    call_command("seed_roles")

    assert set(Group.objects.values_list("name", flat=True)) >= {
        "customer",
        "distributor",
        "admin",
    }


@pytest.mark.django_db
def test_seed_roles_creates_one_stub_user_per_role():
    call_command("seed_roles")

    assert User.objects.filter(groups__name="customer").exists()
    assert User.objects.filter(groups__name="distributor").exists()
    assert User.objects.filter(groups__name="admin").exists()


@pytest.mark.django_db
def test_seed_roles_distributor_stub_has_distributor_row():
    call_command("seed_roles")

    distributor_user = User.objects.get(groups__name="distributor")
    assert Distributor.objects.filter(user=distributor_user).exists()


@pytest.mark.django_db
def test_seed_roles_is_idempotent():
    call_command("seed_roles")
    call_command("seed_roles")

    assert User.objects.filter(groups__name="distributor").count() == 1
