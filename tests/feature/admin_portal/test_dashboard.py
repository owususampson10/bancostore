from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.catalog.models import Category, Product
from apps.distributors.models import DiditVerification, Distributor
from apps.orders.models import Order
from apps.wallet.models import Wallet, WalletTransaction
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_phone_seq = count(1)


def _dashboard_url():
    return reverse("admin_portal:dashboard")


def _make_distributor(
    kyc_status=Distributor.KycStatus.PENDING, with_verification=False
):
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user, phone_number=phone, kyc_status=kyc_status
    )
    if with_verification:
        DiditVerification.objects.create(distributor=distributor, session_id=phone)
    return distributor


def _make_withdrawal_request(distributor, status=WithdrawalRequest.Status.SUBMITTED):
    return WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=Decimal("495.00"),
        status=status,
    )


def _make_product(**overrides):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    defaults = {
        "name": f"Product {Product.objects.count()}",
        "category": category,
        "price": Decimal("100.00"),
        "stock": 20,
        "is_active": True,
    }
    defaults.update(overrides)
    return Product.objects.create(**defaults)


def _make_order(**overrides):
    # order_amounts_sane requires total == subtotal + delivery_fee -- a
    # caller overriding just `total` (the only figure the dashboard
    # tests actually care about) shouldn't also have to work out a
    # matching subtotal by hand.
    delivery_fee = overrides.pop("delivery_fee", Decimal("50.00"))
    if "total" in overrides and "subtotal" not in overrides:
        overrides["subtotal"] = overrides["total"] - delivery_fee
    defaults = {
        "full_name": "Ama Mensah",
        "phone_number": "+233241234567",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High St",
        "area": "Osu",
        "subtotal": Decimal("450.00"),
        "delivery_fee": delivery_fee,
        "total": Decimal("500.00"),
        "pv_earned": 60,
        "payment_reference": f"dashboard-test-{Order.objects.count()}",
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


def _make_wallet_transaction(distributor, **overrides):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    defaults = {
        "wallet": wallet,
        "amount": Decimal("50.00"),
        "transaction_type": WalletTransaction.TransactionType.BINARY_BONUS,
    }
    defaults.update(overrides)
    return WalletTransaction.objects.create(**defaults)


@pytest.mark.django_db
def test_staff_can_reach_the_dashboard(staff_client):
    response = staff_client.get(_dashboard_url())

    assert response.status_code == 200
    assert b"Pending KYC Reviews" in response.content


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    user = User.objects.create_user(
        username="+233249999999", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_dashboard_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_dashboard_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url


@pytest.mark.django_db
def test_logging_out_of_the_admin_portal_redirects_to_the_branded_login_page(
    staff_client,
):
    """Regression test: templates/admin_portal/base_dashboard.html's Log Out
    button used to POST to {% url 'admin:logout' %} -- Django's own
    contrib.admin logout view, which redirects to the raw native
    /admin/login/ page instead of this project's real, branded admin
    login screen. Fixed to POST to account_logout (allauth's real logout
    view, matching the storefront's own established convention) with a
    next field pointing at two_factor:login."""
    login_url = reverse("two_factor:login")

    response = staff_client.post(reverse("account_logout"), {"next": login_url})

    assert response.status_code == 302
    assert response.url == login_url
    assert response.url != reverse("admin:login")


@pytest.mark.django_db
def test_pending_kyc_count_only_counts_submitted_verifications(staff_client):
    _make_distributor(with_verification=True)
    _make_distributor(with_verification=True)
    _make_distributor(with_verification=False)  # not reached KYC step yet
    _make_distributor(
        kyc_status=Distributor.KycStatus.APPROVED, with_verification=True
    )  # already decided

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("Pending KYC Reviews")
    card = body[card_start : card_start + 400]
    assert ">2<" in card


@pytest.mark.django_db
def test_pending_withdrawals_count_only_counts_submitted_status(staff_client):
    distributor = _make_distributor()
    _make_withdrawal_request(distributor, status=WithdrawalRequest.Status.SUBMITTED)
    _make_withdrawal_request(distributor, status=WithdrawalRequest.Status.SUBMITTED)
    _make_withdrawal_request(distributor, status=WithdrawalRequest.Status.PAID)

    response = staff_client.get(_dashboard_url())

    assert b"Pending Withdrawals" in response.content
    # Isolate the withdrawal card's own count rather than asserting a bare
    # "2" that could coincidentally match some other number on the page.
    body = response.content.decode()
    card_start = body.index("Pending Withdrawals")
    card = body[card_start : card_start + 400]
    assert ">2<" in card


@pytest.mark.django_db
def test_orders_awaiting_action_counts_confirmed_and_processing_only(staff_client):
    _make_order(status=Order.Status.CONFIRMED)
    _make_order(status=Order.Status.PROCESSING)
    _make_order(status=Order.Status.PENDING)  # unpaid, nothing to act on yet
    _make_order(status=Order.Status.DELIVERED)  # already done
    _make_order(status=Order.Status.CANCELLED)

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("Orders Awaiting Action")
    card = body[card_start : card_start + 400]
    assert ">2<" in card


@pytest.mark.django_db
def test_low_stock_counts_active_products_under_the_threshold(staff_client):
    _make_product(stock=5, is_active=True)
    _make_product(stock=9, is_active=True)
    _make_product(stock=50, is_active=True)
    _make_product(stock=1, is_active=False)  # inactive, not shown on storefront

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("Low Stock Products")
    card = body[card_start : card_start + 400]
    assert ">2<" in card


@pytest.mark.django_db
def test_total_distributors_and_new_this_week(staff_client):
    _make_distributor()
    _make_distributor()
    old = _make_distributor()
    old.user.date_joined = timezone.now() - timezone.timedelta(days=30)
    old.user.save(update_fields=["date_joined"])

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    assert "3" in body  # total
    assert "+2 this week" in body


@pytest.mark.django_db
def test_total_products_counts_only_active(staff_client):
    _make_product(is_active=True)
    _make_product(is_active=True)
    _make_product(is_active=False)

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("Total Products")
    card = body[card_start : card_start + 400]
    assert ">2<" in card


@pytest.mark.django_db
def test_this_weeks_orders_count_includes_pending_but_value_excludes_it(staff_client):
    _make_order(status=Order.Status.CONFIRMED, total=Decimal("500.00"))
    _make_order(status=Order.Status.PENDING, total=Decimal("999.00"))
    _make_order(status=Order.Status.CANCELLED, total=Decimal("777.00"))

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("This Week's Orders")
    card = body[card_start : card_start + 500]
    assert ">3<" in card  # all 3 orders counted
    assert "500.00" in card  # only the confirmed order's value
    assert "999.00" not in card
    assert "777.00" not in card


@pytest.mark.django_db
def test_this_weeks_orders_excludes_orders_older_than_seven_days(staff_client):
    order = _make_order(status=Order.Status.CONFIRMED, total=Decimal("500.00"))
    Order.objects.filter(pk=order.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=10)
    )
    _make_order(status=Order.Status.CONFIRMED, total=Decimal("200.00"))

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("This Week's Orders")
    card = body[card_start : card_start + 500]
    assert ">1<" in card
    assert "200.00" in card
    assert "500.00" not in card


@pytest.mark.django_db
def test_this_weeks_commissions_sums_only_commission_transaction_types(staff_client):
    distributor = _make_distributor()
    _make_wallet_transaction(
        distributor,
        amount=Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
    )
    _make_wallet_transaction(
        distributor,
        amount=Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
    )
    _make_wallet_transaction(
        distributor,
        amount=Decimal("30.00"),
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    )
    # Not a commission -- must not be counted.
    _make_wallet_transaction(
        distributor,
        amount=Decimal("9999.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
    )

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    card_start = body.index("This Week's Commissions")
    card = body[card_start : card_start + 400]
    assert "180.00" in card
    assert "9999" not in card


@pytest.mark.django_db
def test_recent_orders_table_shows_the_latest_orders_with_status_and_total(
    staff_client,
):
    _make_order(full_name="Kwame Mensah", status=Order.Status.PROCESSING)

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    assert "Kwame Mensah" in body
    assert "Processing" in body


@pytest.mark.django_db
def test_recent_orders_table_shows_at_most_six(staff_client):
    for i in range(8):
        _make_order(full_name=f"Customer {i}", payment_reference=f"ref-{i}")

    response = staff_client.get(_dashboard_url())

    body = response.content.decode()
    assert body.count("Customer ") == 6


@pytest.mark.django_db
def test_recent_orders_link_to_order_detail(staff_client):
    order = _make_order()

    response = staff_client.get(_dashboard_url())

    assert (
        reverse("admin_portal:order_detail", args=[order.pk]).encode()
        in response.content
    )


@pytest.mark.django_db
def test_view_all_orders_links_to_the_order_management_queue(staff_client):
    response = staff_client.get(_dashboard_url())

    assert reverse("admin_portal:order_management_queue").encode() in response.content
