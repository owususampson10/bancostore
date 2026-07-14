from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233249{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_staff_can_see_a_distributors_wallet_balance_and_transactions(staff_client):
    """Shipping-and-launch retrospective (2026-07-14): apps/wallet had no
    Django Admin registration at all -- an admin couldn't view a
    distributor's earnings without a raw database query, unlike every
    other internal ledger model in this codebase (e.g. PvLedgerAdmin)."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet

    response = staff_client.get(reverse("admin:wallet_wallet_change", args=[wallet.pk]))

    assert response.status_code == 200
    body = response.content.decode()
    assert "100.00" in body
    assert "direct_referral_bonus" in body
