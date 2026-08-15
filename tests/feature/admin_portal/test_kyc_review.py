from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from constance import config

from apps.distributors.models import DiditVerification, Distributor, IrIdSequence

User = get_user_model()
_phone_seq = count(1)


@pytest.fixture(autouse=True)
def _ensure_ir_id_sequence_row(db):
    """See tests/feature/distributors/test_kyc_admin.py's identical fixture
    -- a transaction=True test elsewhere in the suite can flush this
    migration-seeded row away before an approve_kyc-exercising test here
    runs."""
    IrIdSequence.objects.get_or_create(pk=1, defaults={"next_number": 1})


def _make_distributor(kyc_status=Distributor.KycStatus.PENDING, full_name=""):
    phone = f"+233249{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, kyc_status=kyc_status, full_name=full_name
    )


def _make_verification(distributor, **kwargs):
    defaults = {
        "distributor": distributor,
        "session_id": f"sess-{distributor.pk}",
        "status": DiditVerification.Status.IN_REVIEW,
    }
    defaults.update(kwargs)
    return DiditVerification.objects.create(**defaults)


def _queue_url():
    return reverse("admin_portal:kyc_review_queue")


def _detail_url(distributor):
    return reverse("admin_portal:kyc_review_detail", args=[distributor.pk])


@pytest.mark.django_db
def test_queue_lists_a_pending_distributor_with_a_submitted_verification(
    staff_client,
):
    distributor = _make_distributor(full_name="Ama Mensah")
    _make_verification(distributor, extracted_full_name="Ama Mensah")

    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    assert b"Ama Mensah" in response.content


@pytest.mark.django_db
def test_queue_falls_back_to_the_extracted_id_name_when_full_name_is_blank(
    staff_client,
):
    """Distributor.full_name is only set at registration -- older or
    incomplete records can have it blank even though Didit already
    extracted a real name from the ID. Falling back to that rather than
    showing "(No name on file)" when a name is available."""
    distributor = _make_distributor(full_name="")
    _make_verification(distributor, extracted_full_name="Kojo Antwi")

    response = staff_client.get(_queue_url())

    assert b"Kojo Antwi" in response.content
    assert b"(No name on file)" not in response.content


@pytest.mark.django_db
def test_detail_falls_back_to_the_extracted_id_name_when_full_name_is_blank(
    staff_client,
):
    distributor = _make_distributor(full_name="")
    _make_verification(distributor, extracted_full_name="Kojo Antwi")

    response = staff_client.get(_detail_url(distributor))

    assert b"Kojo Antwi" in response.content
    assert b"(No name on file)" not in response.content


@pytest.mark.django_db
def test_queue_excludes_a_pending_distributor_with_no_verification_submitted_yet(
    staff_client,
):
    _make_distributor(full_name="No Submission Yet")

    response = staff_client.get(_queue_url())

    assert b"No Submission Yet" not in response.content


@pytest.mark.django_db
def test_detail_404s_for_a_distributor_with_no_verification_submitted_yet(
    staff_client,
):
    """code-review finding (2026-07-23): the queue filters out distributors
    with no DiditVerification, but the detail route is reachable directly
    by pk regardless of that filter -- without the same constraint there,
    a staff user could POST approve straight to this URL and get an IR ID
    assigned with nothing actually submitted to review."""
    distributor = _make_distributor(full_name="No Submission Yet")

    response = staff_client.post(_detail_url(distributor), {"action": "approve"})

    assert response.status_code == 404
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.PENDING
    assert distributor.ir_id is None


@pytest.mark.django_db
def test_queue_excludes_an_already_approved_distributor(staff_client):
    distributor = _make_distributor(
        kyc_status=Distributor.KycStatus.APPROVED, full_name="Already Approved"
    )
    _make_verification(distributor)

    response = staff_client.get(_queue_url())

    assert b"Already Approved" not in response.content


@pytest.mark.django_db
def test_queue_shows_an_empty_state_when_nothing_is_pending(staff_client):
    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    assert b"No pending KYC reviews right now." in response.content


@pytest.mark.django_db
def test_detail_shows_the_didit_verification_data(staff_client):
    distributor = _make_distributor(full_name="Kofi Owusu")
    _make_verification(
        distributor,
        extracted_full_name="Kofi Kwame Owusu",
        extracted_document_number="GHA-123456789",
        face_match_score=94.0,
        liveness_score=98.0,
        warnings=["Low image resolution"],
    )

    response = staff_client.get(_detail_url(distributor))

    body = response.content.decode()
    assert response.status_code == 200
    assert "Kofi Kwame Owusu" in body
    assert "GHA-123456789" in body
    assert "Low image resolution" in body


@pytest.mark.django_db
def test_approving_from_the_detail_screen_matches_the_django_admin_action_result(
    staff_client,
):
    distributor = _make_distributor()
    _make_verification(distributor)

    response = staff_client.post(
        _detail_url(distributor), {"action": "approve"}, follow=True
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED
    assert distributor.ir_id == "IR00001"


@pytest.mark.django_db
def test_approving_kyc_produces_a_queryable_history_record_with_the_real_actor(
    staff_client,
):
    """Task 47d verification: Distributor already carries HistoricalRecords()
    (Task 11) and approve_kyc already writes via .save() (not .update()),
    so this needed no new production code -- only proof that the real,
    request-driven path (this exact view, not a bare service-function call
    in a shell with no request context) resolves the actual admin as
    history_user via HistoryRequestMiddleware, and that the transition is
    genuinely queryable with a timestamp."""
    distributor = _make_distributor()
    _make_verification(distributor)

    staff_client.post(_detail_url(distributor), {"action": "approve"})

    latest = distributor.history.first()
    assert latest.kyc_status == Distributor.KycStatus.APPROVED
    assert latest.history_type == "~"
    assert latest.history_user == User.objects.get(username="staff_tester")
    assert latest.history_date is not None


@pytest.mark.django_db
def test_rejecting_from_the_detail_screen_requires_a_reason(staff_client):
    distributor = _make_distributor()
    _make_verification(distributor)

    response = staff_client.post(_detail_url(distributor), {"action": "reject"})

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.PENDING


@pytest.mark.django_db
def test_rejecting_from_the_detail_screen_with_a_reason_rejects(staff_client):
    distributor = _make_distributor()
    _make_verification(distributor)

    response = staff_client.post(
        _detail_url(distributor),
        {"action": "reject", "reason": "Ghana Card photo is blurry"},
        follow=True,
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.REJECTED
    assert distributor.kyc_rejection_reason == "Ghana Card photo is blurry"


@pytest.mark.django_db
def test_detail_screen_offers_preset_rejection_reasons(staff_client):
    config.KYC_REJECTION_REASONS = "Blurry photo,Selfie mismatch"
    distributor = _make_distributor()
    _make_verification(distributor)

    response = staff_client.get(_detail_url(distributor))

    body = response.content.decode()
    assert "Blurry photo" in body
    assert "Selfie mismatch" in body


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    phone = f"+233249{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_queue_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_queue_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url
