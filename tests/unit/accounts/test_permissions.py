from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

import pytest

from apps.accounts.permissions import is_distributor
from apps.distributors.models import Distributor

User = get_user_model()


@pytest.mark.django_db
def test_distributor_group_member_has_distributor_row_and_passes_check():
    user = User.objects.create_user(username="jane", password="testpass123")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    Distributor.objects.create(user=user)

    assert is_distributor(user)
    assert Distributor.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_customer_group_member_fails_distributor_check():
    user = User.objects.create_user(username="john", password="testpass123")
    customer_group, _ = Group.objects.get_or_create(name="customer")
    user.groups.add(customer_group)

    assert not is_distributor(user)
