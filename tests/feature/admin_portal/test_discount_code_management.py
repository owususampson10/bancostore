import datetime
from decimal import Decimal

from django.urls import reverse

import pytest

from apps.promotions.models import DiscountCode


def _valid_payload(**overrides):
    today = datetime.date.today()
    payload = {
        "code": "SAVE20",
        "discount_type": DiscountCode.DiscountType.FIXED,
        "amount": "20.00",
        "expiry_date": (today + datetime.timedelta(days=30)).isoformat(),
        "audience": DiscountCode.Audience.EVERYONE,
        "max_uses": "100",
        "limit_one_per_customer": "on",
        "is_active": "on",
    }
    payload.update(overrides)
    return payload


def _list_url():
    return reverse("admin_portal:discount_code_list")


def _create_url():
    return reverse("admin_portal:discount_code_create")


def _edit_url(discount_code):
    return reverse("admin_portal:discount_code_edit", args=[discount_code.pk])


def _delete_url(discount_code):
    return reverse("admin_portal:discount_code_delete", args=[discount_code.pk])


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_discount_code_list(client, db):
    response = client.get(_list_url())
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_discount_code_list_shows_an_empty_state_with_no_codes(staff_client):
    response = staff_client.get(_list_url())

    assert response.status_code == 200
    assert b"No discount codes yet" in response.content


@pytest.mark.django_db
def test_discount_code_list_shows_existing_codes(staff_client):
    today = datetime.date.today()
    DiscountCode.objects.create(
        code="WELCOME10",
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        amount=Decimal("10.00"),
        expiry_date=today + datetime.timedelta(days=10),
        max_uses=50,
    )

    response = staff_client.get(_list_url())

    content = response.content.decode()
    assert "WELCOME10" in content


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_create_a_fixed_amount_code(staff_client):
    response = staff_client.post(_create_url(), _valid_payload())

    assert response.status_code == 302
    code = DiscountCode.objects.get()
    assert code.code == "SAVE20"
    assert code.discount_type == DiscountCode.DiscountType.FIXED
    assert code.amount == Decimal("20.00")


@pytest.mark.django_db
def test_staff_can_create_a_percentage_code(staff_client):
    payload = _valid_payload(
        code="TEN10", discount_type=DiscountCode.DiscountType.PERCENTAGE, amount="10.00"
    )

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    code = DiscountCode.objects.get()
    assert code.discount_type == DiscountCode.DiscountType.PERCENTAGE


@pytest.mark.django_db
def test_creating_a_percentage_code_over_100_is_rejected(staff_client):
    payload = _valid_payload(
        discount_type=DiscountCode.DiscountType.PERCENTAGE, amount="150.00"
    )

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not DiscountCode.objects.exists()


@pytest.mark.django_db
def test_creating_a_code_with_zero_amount_is_rejected(staff_client):
    payload = _valid_payload(amount="0")

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not DiscountCode.objects.exists()


@pytest.mark.django_db
def test_creating_a_code_with_zero_max_uses_is_rejected(staff_client):
    payload = _valid_payload(max_uses="0")

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not DiscountCode.objects.exists()


@pytest.mark.django_db
def test_creating_a_duplicate_code_is_rejected(staff_client):
    today = datetime.date.today()
    DiscountCode.objects.create(
        code="SAVE20",
        discount_type=DiscountCode.DiscountType.FIXED,
        amount=Decimal("20.00"),
        expiry_date=today + datetime.timedelta(days=10),
        max_uses=100,
    )

    response = staff_client.post(_create_url(), _valid_payload(code="save20"))

    assert response.status_code == 200
    assert DiscountCode.objects.count() == 1


@pytest.mark.django_db
def test_created_code_defaults_to_limit_one_per_customer_off_when_unchecked(
    staff_client,
):
    payload = _valid_payload()
    del payload["limit_one_per_customer"]

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    code = DiscountCode.objects.get()
    assert code.limit_one_per_customer is False


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_edit_a_codes_max_uses(staff_client):
    today = datetime.date.today()
    code = DiscountCode.objects.create(
        code="SAVE20",
        discount_type=DiscountCode.DiscountType.FIXED,
        amount=Decimal("20.00"),
        expiry_date=today + datetime.timedelta(days=10),
        max_uses=100,
    )

    response = staff_client.post(_edit_url(code), _valid_payload(max_uses="200"))

    assert response.status_code == 302
    code.refresh_from_db()
    assert code.max_uses == 200


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_delete_a_discount_code(staff_client):
    today = datetime.date.today()
    code = DiscountCode.objects.create(
        code="SAVE20",
        discount_type=DiscountCode.DiscountType.FIXED,
        amount=Decimal("20.00"),
        expiry_date=today + datetime.timedelta(days=10),
        max_uses=100,
    )

    response = staff_client.post(_delete_url(code))

    assert response.status_code == 302
    assert not DiscountCode.objects.filter(pk=code.pk).exists()
