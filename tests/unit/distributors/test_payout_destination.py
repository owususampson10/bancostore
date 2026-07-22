from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

import pytest

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_both_fields_blank_by_default():
    distributor = _make_distributor()

    assert distributor.mobile_money_number == ""
    assert distributor.mobile_money_network == ""


@pytest.mark.django_db
def test_both_fields_can_be_set_together():
    distributor = _make_distributor()

    distributor.mobile_money_number = "+233247111222"
    distributor.mobile_money_network = Distributor.MobileMoneyNetwork.MTN
    distributor.save()

    reloaded = Distributor.objects.get(pk=distributor.pk)
    assert reloaded.mobile_money_number == "+233247111222"
    assert reloaded.mobile_money_network == "mtn"


@pytest.mark.django_db
def test_rejects_number_set_without_network_at_db_level():
    distributor = _make_distributor()
    distributor.mobile_money_number = "+233247111222"

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            distributor.save()


@pytest.mark.django_db
def test_rejects_network_set_without_number_at_db_level():
    distributor = _make_distributor()
    distributor.mobile_money_network = Distributor.MobileMoneyNetwork.MTN

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            distributor.save()


@pytest.mark.django_db
def test_has_payout_destination_is_false_when_unset():
    distributor = _make_distributor()

    assert distributor.has_payout_destination is False


@pytest.mark.django_db
def test_has_payout_destination_is_true_once_both_fields_set():
    distributor = _make_distributor()
    distributor.mobile_money_number = "+233247111222"
    distributor.mobile_money_network = Distributor.MobileMoneyNetwork.MTN
    distributor.save()

    assert distributor.has_payout_destination is True
