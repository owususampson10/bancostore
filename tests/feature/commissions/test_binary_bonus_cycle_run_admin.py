from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.urls import reverse

import pytest

from apps.commissions.models import BinaryBonusCycleRun

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)


def _make_cycle_run():
    return BinaryBonusCycleRun.objects.create(
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
        reverse("admin:commissions_binarybonuscyclerun_change", args=[run.pk])
    )

    assert response.status_code == 200
    assert "135.00" in response.content.decode()


@pytest.mark.django_db
def test_staff_cannot_edit_a_cycle_run_even_as_superuser(staff_client):
    """BinaryBonusCycleRunAdmin.has_change_permission returns a hardcoded
    False, which Django's admin checks directly on POST rather than
    through request.user.has_perm(...) -- unlike has_view_permission's
    default, this is NOT subject to the superuser has_perm bypass, even
    though every real admin account in this project is a superuser (see
    tests/conftest.py's staff_client). Verifies that claim end-to-end
    against the real admin view, not just by reading Django's source."""
    run = _make_cycle_run()

    response = staff_client.post(
        reverse("admin:commissions_binarybonuscyclerun_change", args=[run.pk]),
        {"evaluated": 999, "paid": 999, "failed": 999, "total_amount": "1.00"},
    )

    assert response.status_code == 403
    run.refresh_from_db()
    assert run.evaluated == 5
    assert run.total_amount == Decimal("135.00")


@pytest.mark.django_db
def test_staff_cannot_add_a_cycle_run_by_hand(staff_client):
    """This model is exclusively written by apps/commissions/tasks.py::
    calculate_binary_bonus -- an admin-created row would be a fake audit
    entry, defeating the entire point of the audit trail."""
    response = staff_client.get(reverse("admin:commissions_binarybonuscyclerun_add"))

    assert response.status_code == 403
    assert not BinaryBonusCycleRun.objects.exists()


@pytest.mark.django_db
def test_staff_cannot_delete_a_cycle_run_even_as_superuser(staff_client):
    run = _make_cycle_run()

    response = staff_client.post(
        reverse("admin:commissions_binarybonuscyclerun_delete", args=[run.pk])
    )

    assert response.status_code == 403
    assert BinaryBonusCycleRun.objects.filter(pk=run.pk).exists()
