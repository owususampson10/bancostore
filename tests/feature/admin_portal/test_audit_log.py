from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.orders.models import Order
from apps.platform_settings.models import PlatformSettingChange
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_phone_seq = count(1)
_ref_seq = count(1)


def _make_distributor(full_name="Ama Mensah"):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, full_name=full_name
    )


def _make_order():
    return Order.objects.create(
        full_name="Kofi Mensah",
        phone_number="+233241234567",
        email="kofi@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference=f"audit-humanize-test-{next(_ref_seq)}",
        status=Order.Status.PENDING,
    )


def _audit_log_url():
    return reverse("admin_portal:audit_log")


@pytest.mark.django_db
def test_audit_log_shows_a_catalog_change(staff_client):
    Category.objects.create(name="Skincare")

    response = staff_client.get(_audit_log_url())

    assert response.status_code == 200
    body = response.content.decode()
    assert "Category" in body
    assert "Skincare" in body


@pytest.mark.django_db
def test_audit_log_merges_platform_setting_changes_with_model_history(
    staff_client,
):
    """Cross-model correctness: a PlatformSettingChange row (Task 47d,
    not a HistoricalRecords()-tracked model) and a Product history row
    (a real HistoricalRecords()-tracked model) must both appear on the
    same merged, single-screen list."""
    category = Category.objects.create(name="Electronics")
    Product.objects.create(
        name="Wireless Mouse", category=category, price=Decimal("120.00")
    )
    PlatformSettingChange.objects.create(
        key="BINARY_BONUS_RATE", old_value="10", new_value="12"
    )

    response = staff_client.get(_audit_log_url())

    body = response.content.decode()
    assert "Wireless Mouse" in body
    # Task 48 humanization: the raw constance key is never shown --
    # BINARY_BONUS_RATE reads as "Binary Bonus Rate" to a real admin.
    assert "BINARY_BONUS_RATE" not in body
    assert "Binary Bonus Rate" in body
    assert "10 → 12" in body


@pytest.mark.django_db
def test_audit_log_shows_a_distributor_change(staff_client):
    distributor = _make_distributor()
    distributor.rank = "Silver"
    distributor.save()

    response = staff_client.get(_audit_log_url())

    assert "Distributor" in response.content.decode()


@pytest.mark.django_db
def test_audit_log_orders_entries_newest_first(staff_client):
    Category.objects.create(name="First")
    second = Category.objects.create(name="Second")
    second.name = "Second Renamed"
    second.save()

    response = staff_client.get(_audit_log_url())

    body = response.content.decode()
    assert body.index("Second Renamed") < body.index("First")


@pytest.mark.django_db
def test_audit_log_filters_by_date_range_excluding_out_of_range_rows(
    staff_client,
):
    Category.objects.create(name="Old Enough To Exclude")

    response = staff_client.get(
        _audit_log_url(), {"date_from": "2020-01-01", "date_to": "2020-01-02"}
    )

    assert response.status_code == 200
    assert "Old Enough To Exclude" not in response.content.decode()


@pytest.mark.django_db
def test_audit_log_shows_an_empty_state_with_no_entries(staff_client):
    response = staff_client.get(
        _audit_log_url(), {"date_from": "2020-01-01", "date_to": "2020-01-02"}
    )

    assert b"No audit entries in this range." in response.content


@pytest.mark.django_db
def test_audit_log_paginates_at_twenty_per_page(staff_client):
    for i in range(25):
        Category.objects.create(name=f"Category {i}")

    first_page = staff_client.get(_audit_log_url())
    second_page = staff_client.get(_audit_log_url(), {"page": 2})

    assert b"Page 1 of 2" in first_page.content
    assert b"Page 2 of 2" in second_page.content


@pytest.mark.django_db
def test_audit_log_requires_staff(client, db):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_audit_log_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_audit_log_redirects_anonymous_user_to_login(client, db):
    response = client.get(_audit_log_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url


# ---------------------------------------------------------------------------
# Task 48: humanization -- the raw internal identifiers this screen used to
# show were caught live by the user reviewing a real screenshot as too
# technical for a regular admin to read.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_distributor_change_shows_the_full_name_not_the_debug_repr(staff_client):
    distributor = _make_distributor(full_name="Ama Mensah")
    distributor.rank = "Silver"
    distributor.save()

    body = staff_client.get(_audit_log_url()).content.decode()

    assert "Ama Mensah" in body
    # The old debug repr, e.g. "Distributor<+233248000001>" -- not just
    # the substring "Distributor<", which also matches the harmless
    # ">Distributor</span>" model-label tag.
    assert f"Distributor<{distributor.phone_number}>" not in body


@pytest.mark.django_db
def test_a_field_name_in_a_diff_is_shown_humanized(staff_client):
    distributor = _make_distributor()
    distributor.phone_verified = True
    distributor.save()

    body = staff_client.get(_audit_log_url()).content.decode()

    assert "Phone Verified" in body
    assert "phone_verified" not in body


@pytest.mark.django_db
def test_an_order_change_shows_a_friendly_repr_not_the_debug_repr(staff_client):
    order = _make_order()
    order.status = Order.Status.CONFIRMED
    order.save()

    body = staff_client.get(_audit_log_url()).content.decode()

    assert f"Order #{order.pk} (Confirmed)" in body
    # The old debug repr, e.g. "Order<1 confirmed>" -- not just the
    # substring "Order<", which also matches the harmless
    # ">Order</span>" model-label tag.
    assert f"Order<{order.pk} {order.status}>" not in body


@pytest.mark.django_db
def test_a_withdrawal_request_row_shows_a_spaced_model_label(staff_client):
    distributor = _make_distributor()
    withdrawal = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
    )

    body = staff_client.get(_audit_log_url()).content.decode()

    assert "Withdrawal Request" in body
    assert f"Withdrawal #{withdrawal.pk}" in body
    assert "WithdrawalRequest" not in body
