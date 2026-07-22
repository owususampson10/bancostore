from decimal import Decimal
from itertools import count

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
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


def _inline_management_form_data(wallet, existing_transactions):
    """Django admin inline formsets need a full management-form payload
    (TOTAL_FORMS/INITIAL_FORMS/etc.) plus per-row data even for readonly
    fields -- this builds the minimum valid payload for
    WalletTransactionInline's formset, prefix "wallettransaction_set"
    (the model's default related_name-derived prefix)."""
    prefix = "wallettransaction_set"
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(existing_transactions) + 1),
        f"{prefix}-INITIAL_FORMS": str(len(existing_transactions)),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, txn in enumerate(existing_transactions):
        data[f"{prefix}-{i}-id"] = str(txn.pk)
        data[f"{prefix}-{i}-wallet"] = str(wallet.pk)
    return data, prefix


@pytest.mark.django_db
def test_staff_cannot_add_a_wallet_transaction_via_the_admin_inline(staff_client):
    """2026-07-22, Task 15: the ledger's only legitimate writer is
    apps/wallet/services.py's credit()/debit() -- WalletTransactionInline
    must reject an admin-submitted new row, not just hide the fields via
    readonly_fields (a POST can still target hidden/absent field names
    directly; the real guard is has_add_permission)."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet
    existing = list(WalletTransaction.objects.filter(wallet=wallet))
    data, prefix = _inline_management_form_data(wallet, existing)
    # Attempt to add a brand new row as the formset's extra (unsaved) form.
    new_index = len(existing)
    data[f"{prefix}-{new_index}-wallet"] = str(wallet.pk)
    data[f"{prefix}-{new_index}-amount"] = "9999.00"
    data[f"{prefix}-{new_index}-transaction_type"] = (
        WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS
    )
    data[f"{prefix}-{new_index}-reference"] = "admin-injected"

    staff_client.post(reverse("admin:wallet_wallet_change", args=[wallet.pk]), data)

    assert not WalletTransaction.objects.filter(reference="admin-injected").exists()
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("100.00")


@pytest.mark.django_db
def test_staff_cannot_edit_a_wallet_transaction_via_the_admin_inline(staff_client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet
    txn = WalletTransaction.objects.get(wallet=wallet)
    data, prefix = _inline_management_form_data(wallet, [txn])
    # readonly_fields means these submitted values have nowhere to bind on
    # the real form even if has_change_permission were somehow bypassed --
    # this proves the actual persisted row is untouched either way.
    data[f"{prefix}-0-amount"] = "1.00"
    data[f"{prefix}-0-transaction_type"] = (
        WalletTransaction.TransactionType.BINARY_BONUS
    )

    staff_client.post(reverse("admin:wallet_wallet_change", args=[wallet.pk]), data)

    txn.refresh_from_db()
    assert txn.amount == Decimal("100.00")
    assert (
        txn.transaction_type == WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS
    )


@pytest.mark.django_db
def test_staff_cannot_delete_a_wallet_transaction_via_the_admin_inline(staff_client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet
    txn = WalletTransaction.objects.get(wallet=wallet)
    data, prefix = _inline_management_form_data(wallet, [txn])
    data[f"{prefix}-0-DELETE"] = "on"

    staff_client.post(reverse("admin:wallet_wallet_change", args=[wallet.pk]), data)

    assert WalletTransaction.objects.filter(pk=txn.pk).exists()


@pytest.mark.django_db
def test_staff_cannot_edit_a_wallets_balance_directly(staff_client):
    """Already-established readonly_fields=["distributor", "balance"]
    protection -- a regression guard proving a direct POST to change
    Wallet.balance itself has no effect, not just that credit()/debit()
    are the ones that happen to be called in practice."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet
    txn = WalletTransaction.objects.get(wallet=wallet)
    data, _ = _inline_management_form_data(wallet, [txn])
    data["balance"] = "999999.00"

    staff_client.post(reverse("admin:wallet_wallet_change", args=[wallet.pk]), data)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("100.00")


@pytest.mark.django_db
def test_staff_cannot_delete_a_wallet_even_as_superuser(staff_client):
    """Security review (2026-07-22, Task 15): WalletTransactionInline was
    locked down, but WalletAdmin itself had no has_delete_permission
    override -- deleting the parent Wallet row cascades (Wallet.CASCADE on
    WalletTransaction.wallet) and silently destroys the distributor's
    entire ledger, defeating the inline lockdown entirely since it's never
    reached. Mirrors tests/feature/commissions/test_commission_cycle_run_
    admin.py's equivalent test for CommissionCycleRunAdmin."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    wallet = distributor.wallet

    response = staff_client.post(
        reverse("admin:wallet_wallet_delete", args=[wallet.pk])
    )

    assert response.status_code == 403
    assert Wallet.objects.filter(pk=wallet.pk).exists()
    assert WalletTransaction.objects.filter(wallet=wallet).exists()


@pytest.mark.django_db
def test_staff_cannot_add_a_wallet_by_hand(staff_client):
    """A Wallet should only ever come into existence via
    apps/wallet/services.py::credit()'s own get_or_create -- an
    admin-created row bypasses that lazy-creation convention entirely."""
    response = staff_client.get(reverse("admin:wallet_wallet_add"))

    assert response.status_code == 403


def test_wallet_transaction_inline_explicitly_denies_add_change_delete_permission():
    """The four HTTP-level tests above already pass today purely because
    every WalletTransactionInline field happens to be in readonly_fields
    -- Django excludes readonly fields from the bound form entirely, so a
    POST has nothing meaningful to inject regardless. That's real
    protection today, but it's implicit: it would silently stop working
    if a future field were added to WalletTransaction and someone forgot
    to add it to readonly_fields too. This test targets the explicit,
    field-independent guarantee instead -- has_add_permission/
    has_change_permission/has_delete_permission hardcoded False,
    mirroring apps/commissions/admin.py's CommissionCycleRunAdmin from
    Task 13 -- which holds regardless of what readonly_fields contains."""
    from apps.wallet.admin import WalletTransactionInline
    from apps.wallet.models import Wallet

    inline = WalletTransactionInline(Wallet, admin.site)

    assert inline.has_add_permission(request=None, obj=None) is False
    assert inline.has_change_permission(request=None, obj=None) is False
    assert inline.has_delete_permission(request=None, obj=None) is False
