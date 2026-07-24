from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.withdrawal.models import WithdrawalRequest

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    """Mirrors test_earnings_history.py's own helper -- adds the
    "distributor" group so is_distributor() (the guard withdrawal_history
    uses) resolves True, matching a real account's shape."""
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


def _make_request(distributor, status, amount=Decimal("500.00")):
    tax = Decimal("5.00")
    return WithdrawalRequest.objects.create(
        distributor=distributor,
        status=status,
        amount=amount,
        tax_amount=tax,
        net_amount=amount - tax,
    )


@pytest.mark.django_db
def test_withdrawal_history_requires_login(client):
    response = client.get(reverse("distributors:withdrawal_history"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    """Mirrors test_earnings_history.py's own security regression guard:
    a logged-in User with no Distributor row must get a clean 403, not an
    unhandled 500 from request.user.distributor raising DoesNotExist."""
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:withdrawal_history"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_shows_empty_state_when_distributor_has_no_requests(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    assert response.status_code == 200
    assert "No withdrawal requests yet" in response.content.decode()


@pytest.mark.django_db
def test_never_shows_another_distributors_requests(client):
    """There is no id/reference taken from the URL or query params -- the
    query is always scoped to request.user.distributor. This is the
    regression guard proving that scoping actually holds, mirroring
    test_earnings_history.py's own IDOR check."""
    own = _make_distributor()
    other = _make_distributor()
    _make_request(own, WithdrawalRequest.Status.SUBMITTED, amount=Decimal("50.00"))
    _make_request(other, WithdrawalRequest.Status.SUBMITTED, amount=Decimal("999.00"))
    _login(client, own)

    response = client.get(reverse("distributors:withdrawal_history"))

    body = response.content.decode()
    assert "GHS 999.00" not in body


@pytest.mark.django_db
def test_approved_status_says_processes_friday_not_paid(client):
    """Task 16g acceptance criteria: the copy must be honest that
    "approved" does not mean "paid yet" -- a request can sit approved but
    unpaid for up to a week (ADR-0004)."""
    distributor = _make_distributor()
    _make_request(distributor, WithdrawalRequest.Status.APPROVED_DEBITED)
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    body = response.content.decode()
    assert "Approved" in body
    assert "processes Friday" in body


@pytest.mark.django_db
def test_paid_status_is_shown_distinctly_from_approved(client):
    distributor = _make_distributor()
    _make_request(distributor, WithdrawalRequest.Status.PAID)
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    body = response.content.decode()
    assert "Paid" in body
    assert "processes Friday" not in body


@pytest.mark.django_db
def test_rejected_status_shows_the_rejection_reason(client):
    distributor = _make_distributor()
    request = _make_request(distributor, WithdrawalRequest.Status.REJECTED)
    request.rejection_reason = "KYC re-verification required"
    request.save(update_fields=["rejection_reason"])
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    body = response.content.decode()
    assert "Rejected" in body
    assert "KYC re-verification required" in body


@pytest.mark.django_db
def test_payout_failed_reversed_status_mentions_the_wallet(client):
    """Distributor-facing reassurance that the money isn't lost -- it's
    back in their wallet, not the raw model label ("Payout failed
    (reversed)"), which reads alarming without that context."""
    distributor = _make_distributor()
    _make_request(distributor, WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED)
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    assert "returned to wallet" in response.content.decode()


@pytest.mark.django_db
def test_paginates_requests(client):
    distributor = _make_distributor()
    for _ in range(25):
        _make_request(distributor, WithdrawalRequest.Status.SUBMITTED)
    _login(client, distributor)

    page_one = client.get(reverse("distributors:withdrawal_history"))
    page_two = client.get(reverse("distributors:withdrawal_history"), {"page": 2})

    assert page_one.status_code == 200
    assert page_two.status_code == 200
    assert page_one.context["page_obj"].has_next() is True
    assert page_two.context["page_obj"].number == 2
    assert (
        len(page_one.context["page_obj"].object_list)
        == page_one.context["page_obj"].paginator.per_page
    )


@pytest.mark.django_db
def test_withdraw_now_link_points_to_the_submission_page(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:withdrawal_history"))

    assert reverse("distributors:withdrawal_request") in response.content.decode()
