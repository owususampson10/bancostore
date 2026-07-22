from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.urls import reverse

import pytest

from apps.commissions.models import CommissionCycleRun

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)


def _make_cycle_run(job_name="calculate-binary-bonus"):
    return CommissionCycleRun.objects.create(
        job_name=job_name,
        run_at=RUN_AT,
        evaluated=5,
        paid=3,
        failed=0,
        total_amount=Decimal("135.00"),
    )


@pytest.mark.django_db
def test_staff_can_view_a_cycle_run(staff_client):
    """2026-07-22 security-and-hardening review: this audit trail exists
    specifically so a support inquiry doesn't need a raw database query --
    it must actually be visible in Django Admin, same as every other
    internal ledger model in this codebase."""
    run = _make_cycle_run()

    response = staff_client.get(
        reverse("admin:commissions_commissioncyclerun_change", args=[run.pk])
    )

    assert response.status_code == 200
    assert "135.00" in response.content.decode()


@pytest.mark.django_db
def test_staff_cannot_edit_a_cycle_run_even_as_superuser(staff_client):
    """CommissionCycleRunAdmin.has_change_permission returns a hardcoded
    False, which Django's admin checks directly on POST rather than
    through request.user.has_perm(...) -- unlike has_view_permission's
    default, this is NOT subject to the superuser has_perm bypass, even
    though every real admin account in this project is a superuser (see
    tests/conftest.py's staff_client). Verifies that claim end-to-end
    against the real admin view, not just by reading Django's source."""
    run = _make_cycle_run()

    response = staff_client.post(
        reverse("admin:commissions_commissioncyclerun_change", args=[run.pk]),
        {"evaluated": 999, "paid": 999, "failed": 999, "total_amount": "1.00"},
    )

    assert response.status_code == 403
    run.refresh_from_db()
    assert run.evaluated == 5
    assert run.total_amount == Decimal("135.00")


@pytest.mark.django_db
def test_staff_cannot_add_a_cycle_run_by_hand(staff_client):
    """This model is exclusively written by apps/commissions/tasks.py::
    _persist_cycle_audit_record -- an admin-created row would be a fake
    audit entry, defeating the entire point of the audit trail."""
    response = staff_client.get(reverse("admin:commissions_commissioncyclerun_add"))

    assert response.status_code == 403
    assert not CommissionCycleRun.objects.exists()


@pytest.mark.django_db
def test_staff_cannot_delete_a_cycle_run_even_as_superuser(staff_client):
    run = _make_cycle_run()

    response = staff_client.post(
        reverse("admin:commissions_commissioncyclerun_delete", args=[run.pk])
    )

    assert response.status_code == 403
    assert CommissionCycleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_list_view_shows_which_job_produced_each_row(staff_client):
    """job_name is what makes this table useful across bonus types instead
    of one copy-pasted admin per job -- it must actually be visible/
    filterable in the changelist, not just stored."""
    _make_cycle_run(job_name="calculate-binary-bonus")
    _make_cycle_run(job_name="calculate-matching-bonus")

    response = staff_client.get(
        reverse("admin:commissions_commissioncyclerun_changelist")
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "calculate-binary-bonus" in body
    assert "calculate-matching-bonus" in body
