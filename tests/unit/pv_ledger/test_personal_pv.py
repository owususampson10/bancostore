from datetime import date, datetime
from datetime import timezone as dt_timezone
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv
from apps.pv_ledger.services import is_eligible_for_binary_bonus, record_personal_pv

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233243{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _at(iso_date):
    return datetime.fromisoformat(iso_date).replace(tzinfo=dt_timezone.utc)


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_record_personal_pv_creates_a_row_for_the_current_month(mock_now):
    mock_now.return_value = _at("2026-07-14")
    distributor = _make_distributor()

    record_personal_pv(distributor, 60)

    row = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert row.period == date(2026, 7, 1)
    assert row.pv == 60


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_a_second_credit_in_the_same_month_adds_rather_than_overwrites(mock_now):
    mock_now.return_value = _at("2026-07-01")
    distributor = _make_distributor()

    record_personal_pv(distributor, 60)
    mock_now.return_value = _at("2026-07-28")
    record_personal_pv(distributor, 50)

    row = MonthlyPersonalPv.objects.get(
        distributor=distributor, period=date(2026, 7, 1)
    )
    assert row.pv == 110


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_a_credit_in_a_different_month_creates_a_separate_row(mock_now):
    mock_now.return_value = _at("2026-06-30")
    distributor = _make_distributor()

    record_personal_pv(distributor, 60)
    mock_now.return_value = _at("2026-07-01")
    record_personal_pv(distributor, 50)

    assert MonthlyPersonalPv.objects.filter(distributor=distributor).count() == 2
    june = MonthlyPersonalPv.objects.get(
        distributor=distributor, period=date(2026, 6, 1)
    )
    july = MonthlyPersonalPv.objects.get(
        distributor=distributor, period=date(2026, 7, 1)
    )
    assert june.pv == 60
    assert july.pv == 50


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_distributor_below_the_threshold_is_not_eligible(mock_now):
    mock_now.return_value = _at("2026-07-14")
    distributor = _make_distributor()

    record_personal_pv(distributor, config.MIN_MONTHLY_PERSONAL_PV - 1)

    assert is_eligible_for_binary_bonus(distributor) is False


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_distributor_exactly_at_the_threshold_is_eligible(mock_now):
    mock_now.return_value = _at("2026-07-14")
    distributor = _make_distributor()

    record_personal_pv(distributor, config.MIN_MONTHLY_PERSONAL_PV)

    assert is_eligible_for_binary_bonus(distributor) is True


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_distributor_with_no_personal_pv_this_month_is_not_eligible(mock_now):
    mock_now.return_value = _at("2026-07-14")
    distributor = _make_distributor()

    assert is_eligible_for_binary_bonus(distributor) is False


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_last_months_pv_does_not_count_toward_this_months_eligibility(mock_now):
    mock_now.return_value = _at("2026-06-14")
    distributor = _make_distributor()

    record_personal_pv(distributor, config.MIN_MONTHLY_PERSONAL_PV)

    mock_now.return_value = _at("2026-07-14")
    assert is_eligible_for_binary_bonus(distributor) is False
