from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from constance import config

from apps.catalog.models import Category, Product
from apps.compliance.tasks import cleanup_expired_audit_records
from apps.distributors.models import Distributor
from apps.orders.models import Order
from apps.platform_settings.models import PlatformSettingChange
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_ref_seq = count(1)


def _make_distributor():
    phone = f"+233248{next(_ref_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_order():
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference=f"audit-cleanup-test-{next(_ref_seq)}",
        status=Order.Status.CONFIRMED,
    )


def _make_withdrawal_request(distributor):
    return WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )


def _age_history_row(history_row, days):
    type(history_row).objects.filter(pk=history_row.pk).update(
        history_date=timezone.now() - timedelta(days=days)
    )


@pytest.mark.django_db
def test_cleanup_deletes_history_older_than_the_retention_window_across_every_source():
    config.AUDIT_LOG_RETENTION_PERIOD_DAYS = 90
    distributor = _make_distributor()
    order = _make_order()
    withdrawal = _make_withdrawal_request(distributor)
    category = Category.objects.create(name="Old Category")
    product = Product.objects.create(
        name="Old Product", category=category, price=Decimal("50.00")
    )
    change = PlatformSettingChange.objects.create(
        key="SOME_SETTING", old_value="1", new_value="2"
    )

    # Age every source's most recent history row past the 90-day window.
    _age_history_row(distributor.history.first(), days=120)
    _age_history_row(order.history.first(), days=120)
    _age_history_row(withdrawal.history.first(), days=120)
    _age_history_row(category.history.first(), days=120)
    _age_history_row(product.history.first(), days=120)
    PlatformSettingChange.objects.filter(pk=change.pk).update(
        changed_at=timezone.now() - timedelta(days=120)
    )

    result = cleanup_expired_audit_records()

    assert result == {
        "Category": 1,
        "Product": 1,
        "Distributor": 1,
        "Order": 1,
        "WithdrawalRequest": 1,
        # Task 48a added NotificationTemplate to the tracked-models dict
        # this task reuses -- 0 here since this test creates none, not a
        # sign the new model is untracked.
        "NotificationTemplate": 0,
        "PlatformSettingChange": 1,
    }
    assert not distributor.history.exists()
    assert not order.history.exists()
    assert not withdrawal.history.exists()
    assert not category.history.exists()
    assert not product.history.exists()
    assert not PlatformSettingChange.objects.filter(pk=change.pk).exists()


@pytest.mark.django_db
def test_cleanup_keeps_history_within_the_retention_window():
    config.AUDIT_LOG_RETENTION_PERIOD_DAYS = 90
    distributor = _make_distributor()
    category = Category.objects.create(name="Recent Category")
    change = PlatformSettingChange.objects.create(
        key="SOME_SETTING", old_value="1", new_value="2"
    )
    _age_history_row(distributor.history.first(), days=30)
    _age_history_row(category.history.first(), days=30)
    PlatformSettingChange.objects.filter(pk=change.pk).update(
        changed_at=timezone.now() - timedelta(days=30)
    )

    result = cleanup_expired_audit_records()

    assert result["Distributor"] == 0
    assert result["Category"] == 0
    assert result["PlatformSettingChange"] == 0
    assert distributor.history.exists()
    assert category.history.exists()
    assert PlatformSettingChange.objects.filter(pk=change.pk).exists()


@pytest.mark.django_db
def test_cleanup_respects_the_live_admin_configured_retention_window():
    """A shorter retention window means a row that would have survived
    under the default is deleted instead -- proving the task reads
    AUDIT_LOG_RETENTION_PERIOD_DAYS live, not a hardcoded constant."""
    config.AUDIT_LOG_RETENTION_PERIOD_DAYS = 30
    category = Category.objects.create(name="Category")
    _age_history_row(category.history.first(), days=45)

    result = cleanup_expired_audit_records()

    assert result["Category"] == 1
    assert not category.history.exists()
