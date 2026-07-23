from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_phone_seq = count(1)


def _make_eligible_distributor(balance=Decimal("1000.00")):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    if balance > 0:
        credit(
            distributor,
            balance,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference=f"seed-{distributor.pk}",
        )
    return distributor


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_withdrawal_request_requires_login(client):
    response = client.get(reverse("distributors:withdrawal_request"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:withdrawal_request"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_can_submit_a_valid_withdrawal_request(client):
    distributor = _make_eligible_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"),
        {"amount": "500.00"},
        follow=True,
    )

    assert response.status_code == 200
    request = WithdrawalRequest.objects.get(distributor=distributor)
    assert request.amount == Decimal("500.00")
    assert request.tax_amount == Decimal("5.00")
    assert request.net_amount == Decimal("495.00")
    assert "Withdrawal request submitted" in response.content.decode()


@pytest.mark.django_db
def test_shows_kyc_not_approved_error(client):
    distributor = _make_eligible_distributor()
    distributor.kyc_status = Distributor.KycStatus.PENDING
    distributor.save()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "500.00"}
    )

    assert response.status_code == 200
    assert "KYC" in response.content.decode()
    assert not WithdrawalRequest.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db
def test_shows_payout_destination_not_set_error(client):
    distributor = _make_eligible_distributor()
    distributor.mobile_money_number = ""
    distributor.mobile_money_network = ""
    distributor.save()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "500.00"}
    )

    assert response.status_code == 200
    assert "payout destination" in response.content.decode().lower()
    assert not WithdrawalRequest.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db
def test_shows_below_minimum_error(client):
    distributor = _make_eligible_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "50.00"}
    )

    assert response.status_code == 200
    assert "minimum" in response.content.decode().lower()


@pytest.mark.django_db
def test_shows_above_maximum_error(client):
    distributor = _make_eligible_distributor(balance=Decimal("20000.00"))
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "15000.00"}
    )

    assert response.status_code == 200
    assert "maximum" in response.content.decode().lower()


@pytest.mark.django_db
def test_shows_insufficient_balance_error(client):
    distributor = _make_eligible_distributor(balance=Decimal("200.00"))
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "500.00"}
    )

    assert response.status_code == 200
    assert "balance" in response.content.decode().lower()


@pytest.mark.django_db
def test_shows_window_active_error(client):
    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    _login(client, distributor)
    client.post(reverse("distributors:withdrawal_request"), {"amount": "500.00"})

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "200.00"}
    )

    assert response.status_code == 200
    body = response.content.decode().lower()
    assert "already" in body or "recently" in body
    assert WithdrawalRequest.objects.filter(distributor=distributor).count() == 1


@pytest.mark.django_db
def test_rejects_non_numeric_amount(client):
    distributor = _make_eligible_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:withdrawal_request"), {"amount": "not-a-number"}
    )

    assert response.status_code == 200
    assert not WithdrawalRequest.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db
def test_shows_own_wallet_balance(client):
    distributor = _make_eligible_distributor(balance=Decimal("1234.56"))
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_request"))

    assert "1,234.56" in response.content.decode()


@pytest.mark.django_db
def test_never_shows_or_uses_another_distributors_balance(client):
    own = _make_eligible_distributor(balance=Decimal("50.00"))
    _make_eligible_distributor(balance=Decimal("99999.00"))
    _login(client, own)

    response = client.get(reverse("distributors:withdrawal_request"))

    assert "99,999.00" not in response.content.decode()


@pytest.mark.django_db
def test_shows_a_clean_message_not_a_500_when_tax_rate_is_misconfigured(client):
    """code-review-and-quality (2026-07-23): submit_withdrawal_request now
    raises WithholdingTaxMisconfigured (not a raw IntegrityError) for this
    case -- this proves the view actually catches it and shows a clean
    message instead of an unhandled 500."""
    from constance import config

    distributor = _make_eligible_distributor()
    _login(client, distributor)
    original_rate = config.WITHHOLDING_TAX_RATE
    config.WITHHOLDING_TAX_RATE = Decimal("150")
    try:
        response = client.post(
            reverse("distributors:withdrawal_request"), {"amount": "500.00"}
        )
    finally:
        config.WITHHOLDING_TAX_RATE = original_rate

    assert response.status_code == 200
    assert "temporarily unavailable" in response.content.decode().lower()
    assert not WithdrawalRequest.objects.filter(distributor=distributor).exists()
