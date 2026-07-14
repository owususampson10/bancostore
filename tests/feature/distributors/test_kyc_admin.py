from itertools import count

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from constance import config

from apps.distributors.models import DiditVerification, Distributor, IrIdSequence

User = get_user_model()
_phone_seq = count(1)


@pytest.fixture(autouse=True)
def _ensure_ir_id_sequence_row(db):
    """See the identical fixture in tests/unit/distributors/
    test_kyc_review.py for the full explanation -- a transaction=True test
    elsewhere in the suite can flush this migration-seeded row away before
    this file's approve_kyc-exercising test runs."""
    IrIdSequence.objects.get_or_create(pk=1, defaults={"next_number": 1})


def _make_distributor(kyc_status=Distributor.KycStatus.PENDING):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, kyc_status=kyc_status
    )


def _changelist_url():
    return reverse("admin:distributors_distributor_changelist")


@pytest.mark.django_db
def test_staff_can_approve_kyc_via_the_bulk_admin_action(staff_client):
    distributor = _make_distributor()

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "approve_selected_kyc",
            ACTION_CHECKBOX_NAME: [str(distributor.pk)],
        },
        follow=True,
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id == "IR00001"


@pytest.mark.django_db
def test_reject_action_first_shows_a_confirmation_page_asking_for_a_reason(
    staff_client,
):
    distributor = _make_distributor()

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected_kyc",
            ACTION_CHECKBOX_NAME: [str(distributor.pk)],
        },
    )

    assert response.status_code == 200
    assert b"reason" in response.content.lower()
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.PENDING  # not yet rejected


@pytest.mark.django_db
def test_reject_action_with_a_reason_rejects_the_distributor(staff_client):
    distributor = _make_distributor()

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected_kyc",
            ACTION_CHECKBOX_NAME: [str(distributor.pk)],
            "reason": "Ghana Card photo is blurry",
        },
        follow=True,
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.REJECTED
    assert distributor.kyc_rejection_reason == "Ghana Card photo is blurry"
    assert distributor.ir_id is None


@pytest.mark.django_db
def test_reject_confirmation_page_offers_preset_reasons(staff_client):
    config.KYC_REJECTION_REASONS = "Blurry photo,Selfie mismatch"
    distributor = _make_distributor()

    response = staff_client.post(
        _changelist_url(),
        {
            "action": "reject_selected_kyc",
            ACTION_CHECKBOX_NAME: [str(distributor.pk)],
        },
    )

    body = response.content.decode()
    assert "Blurry photo" in body
    assert "Selfie mismatch" in body


@pytest.mark.django_db
def test_distributor_change_page_shows_didit_result(staff_client):
    distributor = _make_distributor()
    DiditVerification.objects.create(
        distributor=distributor,
        session_id="sess-admin-view",
        status=DiditVerification.Status.APPROVED,
        extracted_full_name="Ama Mensah",
    )

    response = staff_client.get(
        reverse("admin:distributors_distributor_change", args=[distributor.pk])
    )

    assert response.status_code == 200
    assert "Ama Mensah" in response.content.decode()
