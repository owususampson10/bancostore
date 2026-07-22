from decimal import Decimal
from itertools import count

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_request(distributor):
    from apps.withdrawal.models import WithdrawalRequest

    return WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )


@pytest.mark.django_db
def test_staff_can_view_a_withdrawal_request(staff_client):
    """tests/conftest.py::staff_client is this project's only real admin
    shape (is_superuser=True -- 'Bancostore admin is a single flat role,'
    not a granular permission system). has_view_permission's default
    resolves True for a superuser independent of has_change_permission,
    so this must succeed even though change/add/delete are all locked
    below."""
    distributor = _make_distributor()
    request = _make_request(distributor)

    response = staff_client.get(
        reverse("admin:withdrawal_withdrawalrequest_change", args=[request.pk])
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "500.00" in body


@pytest.mark.django_db
def test_staff_can_see_the_changelist(staff_client):
    distributor = _make_distributor()
    _make_request(distributor)

    response = staff_client.get(
        reverse("admin:withdrawal_withdrawalrequest_changelist")
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_staff_cannot_add_a_withdrawal_request_even_as_superuser(staff_client):
    """WithdrawalRequest's only legitimate writer is Task 16c's
    submit_withdrawal_request -- an admin-created row bypasses tax
    calculation, the once-per-window check, and every other invariant
    that service enforces."""
    response = staff_client.get(reverse("admin:withdrawal_withdrawalrequest_add"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_staff_cannot_edit_a_withdrawal_request_even_as_superuser(staff_client):
    distributor = _make_distributor()
    request = _make_request(distributor)

    response = staff_client.post(
        reverse("admin:withdrawal_withdrawalrequest_change", args=[request.pk]),
        {"amount": "999999.00", "status": "paid"},
    )

    assert response.status_code == 403
    request.refresh_from_db()
    assert request.amount == Decimal("500.00")
    assert request.status == "submitted"


@pytest.mark.django_db
def test_staff_cannot_delete_a_withdrawal_request_even_as_superuser(staff_client):
    """Mirrors WalletAdmin's equivalent test (Task 15) -- a deletable audit
    row defeats the whole point of tracking withdrawal history."""
    distributor = _make_distributor()
    request = _make_request(distributor)

    response = staff_client.post(
        reverse("admin:withdrawal_withdrawalrequest_delete", args=[request.pk])
    )

    assert response.status_code == 403
    from apps.withdrawal.models import WithdrawalRequest

    assert WithdrawalRequest.objects.filter(pk=request.pk).exists()


def test_withdrawal_request_admin_explicitly_denies_add_change_delete_permission():
    """Field-independent guarantee (mirrors WalletAdmin/CommissionCycleRunAdmin
    exactly) -- holds regardless of what readonly_fields contains, and
    regardless of is_superuser, since these overrides never consult it."""
    from apps.withdrawal.admin import WithdrawalRequestAdmin
    from apps.withdrawal.models import WithdrawalRequest

    model_admin = WithdrawalRequestAdmin(WithdrawalRequest, admin.site)

    assert model_admin.has_add_permission(request=None) is False
    assert model_admin.has_change_permission(request=None, obj=None) is False
    assert model_admin.has_delete_permission(request=None, obj=None) is False
