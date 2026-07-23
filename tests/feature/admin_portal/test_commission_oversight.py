from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.commissions.models import CommissionCycleFailure, CommissionCycleRun
from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(full_name="Ama Mensah"):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, full_name=full_name
    )


def _make_cycle_run(
    job_name="calculate-binary-bonus",
    run_at=None,
    evaluated=10,
    paid=10,
    failed=0,
    total_amount=Decimal("500.00"),
):
    return CommissionCycleRun.objects.create(
        job_name=job_name,
        run_at=run_at or timezone.now(),
        evaluated=evaluated,
        paid=paid,
        failed=failed,
        total_amount=total_amount,
    )


def _oversight_url():
    return reverse("admin_portal:commission_oversight")


def _detail_url(cycle_run):
    return reverse("admin_portal:commission_cycle_detail", args=[cycle_run.pk])


@pytest.mark.django_db
def test_oversight_lists_recent_batch_runs(staff_client):
    _make_cycle_run(job_name="calculate-binary-bonus")

    response = staff_client.get(_oversight_url())

    assert response.status_code == 200
    assert b"Binary Bonus" in response.content


@pytest.mark.django_db
def test_oversight_shows_matching_bonus_job_label(staff_client):
    _make_cycle_run(job_name="calculate-matching-bonus")

    response = staff_client.get(_oversight_url())

    assert b"Matching Bonus" in response.content


@pytest.mark.django_db
def test_oversight_shows_real_lifetime_totals_per_bonus_type(staff_client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    credit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="ref-2",
    )
    credit(
        distributor,
        Decimal("25.00"),
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
        reference="ref-3",
    )

    response = staff_client.get(_oversight_url())

    body = response.content.decode()
    assert "GHS 100.00" in body
    assert "GHS 50.00" in body
    assert "GHS 25.00" in body


@pytest.mark.django_db
def test_oversight_shows_zero_totals_when_nothing_has_been_paid(staff_client):
    response = staff_client.get(_oversight_url())

    assert response.status_code == 200
    body = response.content.decode()
    assert body.count("GHS 0.00") == 3


@pytest.mark.django_db
def test_oversight_shows_an_empty_state_with_no_batch_runs(staff_client):
    response = staff_client.get(_oversight_url())

    assert response.status_code == 200
    assert b"No batch runs yet." in response.content


@pytest.mark.django_db
def test_oversight_only_shows_view_details_for_runs_with_failures(staff_client):
    _make_cycle_run(failed=0)
    failing_run = _make_cycle_run(failed=2)

    response = staff_client.get(_oversight_url())

    body = response.content.decode()
    assert body.count("View Details") == 1
    assert _detail_url(failing_run) in body


@pytest.mark.django_db
def test_oversight_paginates_at_twenty_per_page(staff_client):
    for _ in range(25):
        _make_cycle_run()

    first_page = staff_client.get(_oversight_url())
    second_page = staff_client.get(_oversight_url(), {"page": 2})

    assert b"Page 1 of 2" in first_page.content
    assert b"Page 2 of 2" in second_page.content


@pytest.mark.django_db
def test_oversight_requires_staff(client, db):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_oversight_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_oversight_redirects_anonymous_user_to_login(client, db):
    response = client.get(_oversight_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url


@pytest.mark.django_db
def test_detail_shows_cycle_run_stats(staff_client):
    cycle_run = _make_cycle_run(evaluated=142, paid=140, failed=2)

    response = staff_client.get(_detail_url(cycle_run))

    body = response.content.decode()
    assert response.status_code == 200
    assert "142" in body
    assert "140" in body


@pytest.mark.django_db
def test_detail_lists_failed_distributors_with_raw_error_text(staff_client):
    cycle_run = _make_cycle_run(failed=1)
    CommissionCycleFailure.objects.create(
        cycle_run=cycle_run,
        distributor_id=99812,
        error="ValueError: weak_leg_pv is negative for distributor 99812",
    )

    response = staff_client.get(_detail_url(cycle_run))

    body = response.content.decode()
    assert "99812" in body
    assert "ValueError: weak_leg_pv is negative for distributor 99812" in body


@pytest.mark.django_db
def test_detail_survives_a_failure_whose_distributor_was_deleted(staff_client):
    """CommissionCycleFailure.distributor_id is deliberately not a
    ForeignKey (see the model's own docstring) -- this must render even
    when no Distributor row with that id exists at all."""
    cycle_run = _make_cycle_run(failed=1)
    CommissionCycleFailure.objects.create(
        cycle_run=cycle_run,
        distributor_id=999999,
        error="Distributor.DoesNotExist",
    )

    response = staff_client.get(_detail_url(cycle_run))

    assert response.status_code == 200
    assert b"999999" in response.content


@pytest.mark.django_db
def test_detail_shows_no_failures_state_for_a_clean_run(staff_client):
    cycle_run = _make_cycle_run(failed=0)

    response = staff_client.get(_detail_url(cycle_run))

    assert b"No failures in this run." in response.content


@pytest.mark.django_db
def test_detail_404s_for_a_nonexistent_cycle_run(staff_client):
    response = staff_client.get(
        reverse("admin_portal:commission_cycle_detail", args=[999999])
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_detail_requires_staff(client, db):
    cycle_run = _make_cycle_run(failed=1)
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_detail_url(cycle_run))

    assert response.status_code == 403
