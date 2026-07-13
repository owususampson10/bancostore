import datetime
from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.distributors.models import DiditVerification, Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_fields_round_trip_correctly():
    distributor = _make_distributor()

    verification = DiditVerification.objects.create(
        distributor=distributor,
        session_id="sess-abc",
        status=DiditVerification.Status.APPROVED,
        id_verification_status="Approved",
        face_match_status="Approved",
        face_match_score=0.97,
        liveness_status="Approved",
        liveness_score=0.99,
        extracted_full_name="Ama Mensah",
        extracted_document_number="GHA-123456789-0",
        extracted_date_of_birth=datetime.date(1990, 5, 1),
        warnings=[{"risk": "NONE"}],
    )

    reloaded = DiditVerification.objects.get(pk=verification.pk)
    assert reloaded.distributor_id == distributor.pk
    assert reloaded.session_id == "sess-abc"
    assert reloaded.status == DiditVerification.Status.APPROVED
    assert reloaded.face_match_score == 0.97
    assert reloaded.extracted_full_name == "Ama Mensah"
    assert reloaded.extracted_date_of_birth == datetime.date(1990, 5, 1)
    assert reloaded.warnings == [{"risk": "NONE"}]


@pytest.mark.django_db
def test_defaults_to_pending_status():
    distributor = _make_distributor()

    verification = DiditVerification.objects.create(
        distributor=distributor, session_id="sess-xyz"
    )

    assert verification.status == DiditVerification.Status.PENDING


@pytest.mark.django_db
def test_one_verification_per_distributor():
    distributor = _make_distributor()
    DiditVerification.objects.create(distributor=distributor, session_id="sess-1")

    with pytest.raises(Exception):
        DiditVerification.objects.create(distributor=distributor, session_id="sess-2")


@pytest.mark.django_db
def test_session_id_is_unique():
    first = _make_distributor()
    second = _make_distributor()
    DiditVerification.objects.create(distributor=first, session_id="sess-shared")

    with pytest.raises(Exception):
        DiditVerification.objects.create(distributor=second, session_id="sess-shared")
