import datetime
import io
from itertools import count
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model

import pytest
from PIL import Image

from apps.distributors.didit import DiditError
from apps.distributors.models import DiditVerification, Distributor
from apps.distributors.services import _assert_safe_media_url, consume_didit_result

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_verification(session_id="sess-1"):
    distributor = _make_distributor()
    return DiditVerification.objects.create(
        distributor=distributor, session_id=session_id
    )


def _approved_decision(**overrides):
    decision = {
        "session_id": "sess-1",
        "status": "Approved",
        "id_verifications": [
            {
                "status": "Approved",
                "full_name": "Ama Mensah",
                "document_number": "GHA-123456789-0",
                "date_of_birth": "1990-05-01",
                "front_image": "https://cdn.didit.me/front.jpg",
                "back_image": "https://cdn.didit.me/back.jpg",
                "portrait_image": "https://cdn.didit.me/selfie.jpg",
            }
        ],
        "face_matches": [{"status": "Approved", "score": 0.97}],
        "liveness_checks": [
            {
                "status": "Approved",
                "score": 0.99,
                "reference_image": "https://cdn.didit.me/liveness-selfie.jpg",
            }
        ],
        "warnings": [],
    }
    decision.update(overrides)
    return decision


def _fake_image_http_response():
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(buffer, format="JPEG")
    response = Mock()
    response.content = buffer.getvalue()
    response.raise_for_status.return_value = None
    return response


@pytest.mark.django_db
@patch("apps.distributors.services.socket.gethostbyname", return_value="8.8.8.8")
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_approved_result_updates_status_and_extracted_fields(
    mock_decision, mock_get, mock_dns
):
    verification = _make_verification()
    mock_decision.return_value = _approved_decision()
    mock_get.return_value = _fake_image_http_response()

    consume_didit_result("sess-1")

    verification.refresh_from_db()
    assert verification.status == DiditVerification.Status.APPROVED
    assert verification.id_verification_status == "Approved"
    assert verification.face_match_status == "Approved"
    assert verification.face_match_score == 0.97
    assert verification.liveness_status == "Approved"
    assert verification.liveness_score == 0.99
    assert verification.extracted_full_name == "Ama Mensah"
    assert verification.extracted_document_number == "GHA-123456789-0"
    assert verification.extracted_date_of_birth == datetime.date(1990, 5, 1)


@pytest.mark.django_db
@patch("apps.distributors.services.socket.gethostbyname", return_value="8.8.8.8")
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_images_are_downloaded_and_converted_to_webp(mock_decision, mock_get, mock_dns):
    verification = _make_verification()
    mock_decision.return_value = _approved_decision()
    mock_get.return_value = _fake_image_http_response()

    consume_didit_result("sess-1")

    verification.refresh_from_db()
    assert verification.id_front_image.name.endswith(".webp")
    assert verification.id_back_image.name.endswith(".webp")
    assert verification.selfie_image.name.endswith(".webp")


@pytest.mark.django_db
@patch("apps.distributors.services.socket.gethostbyname", return_value="8.8.8.8")
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_selfie_image_is_downloaded_from_the_liveness_check_not_the_id_document(
    mock_decision, mock_get, mock_dns
):
    """Bug found via a live Didit session (2026-07-23): id_verifications[0]
    .portrait_image is the ID document's own embedded photo crop, not the
    live selfie the distributor actually captured during the liveness
    check -- confirmed by opening both downloaded files and seeing the
    same ID-card background pattern behind the "selfie". The real live
    capture is liveness_checks[0].reference_image. Showing an admin the
    ID's own photo under "Selfie (Liveness)" defeats manual KYC review
    entirely -- there's nothing left to visually compare against the ID."""
    _make_verification()
    mock_decision.return_value = _approved_decision()
    mock_get.return_value = _fake_image_http_response()

    consume_didit_result("sess-1")

    requested_urls = [call.args[0] for call in mock_get.call_args_list]
    assert "https://cdn.didit.me/liveness-selfie.jpg" in requested_urls
    assert "https://cdn.didit.me/selfie.jpg" not in requested_urls


@pytest.mark.django_db
@patch("apps.distributors.services.get_session_decision")
def test_in_progress_status_is_a_no_op(mock_decision):
    verification = _make_verification()
    mock_decision.return_value = {"session_id": "sess-1", "status": "Not Started"}

    consume_didit_result("sess-1")

    verification.refresh_from_db()
    assert verification.status == DiditVerification.Status.PENDING


@pytest.mark.django_db
@patch("apps.distributors.services.socket.gethostbyname", return_value="8.8.8.8")
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_calling_twice_is_idempotent(mock_decision, mock_get, mock_dns):
    _make_verification()
    mock_decision.return_value = _approved_decision()
    mock_get.return_value = _fake_image_http_response()

    consume_didit_result("sess-1")
    consume_didit_result("sess-1")

    assert mock_decision.call_count == 1  # second call short-circuits


@pytest.mark.django_db
def test_missing_verification_does_not_crash():
    consume_didit_result("no-such-session")  # must not raise


@pytest.mark.django_db
@patch("apps.distributors.services.get_session_decision")
def test_didit_error_does_not_crash(mock_decision):
    verification = _make_verification()
    mock_decision.side_effect = DiditError("timed out")

    consume_didit_result("sess-1")  # must not raise

    verification.refresh_from_db()
    assert verification.status == DiditVerification.Status.PENDING


@pytest.mark.django_db
@patch("apps.distributors.services.get_session_decision")
def test_declined_result_never_touches_distributor_kyc_status(mock_decision):
    verification = _make_verification()
    mock_decision.return_value = _approved_decision(
        status="Declined",
        id_verifications=[{"status": "Declined"}],
        face_matches=[{}],
        liveness_checks=[{}],
    )

    consume_didit_result("sess-1")

    verification.refresh_from_db()
    assert verification.status == DiditVerification.Status.DECLINED
    verification.distributor.refresh_from_db()
    assert verification.distributor.kyc_status == Distributor.KycStatus.PENDING


@pytest.mark.django_db
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_missing_image_url_is_skipped_without_crashing(mock_decision, mock_get):
    _make_verification()
    mock_decision.return_value = _approved_decision(
        id_verifications=[{"status": "Approved"}]  # no image URLs at all
    )

    consume_didit_result("sess-1")  # must not raise


@pytest.mark.django_db
def test_malformed_decision_response_does_not_crash(caplog):
    """Third-party response shape is untrusted, not just its content -- a
    non-dict entry in id_verifications[] (a Didit-side bug, or one of our
    own UNVERIFIED field-name assumptions turning out wrong) must not
    propagate an unhandled AttributeError out of a webhook handler."""
    verification = _make_verification()
    with patch("apps.distributors.services.get_session_decision") as mock_decision:
        mock_decision.return_value = _approved_decision(
            id_verifications=["this-should-be-a-dict-not-a-string"]
        )

        consume_didit_result("sess-1")  # must not raise

    verification.refresh_from_db()
    assert verification.status == DiditVerification.Status.PENDING
    assert "unexpected decision response shape" in caplog.text


@pytest.mark.django_db
@patch("apps.distributors.services.requests.get")
def test_image_url_pointing_at_an_unexpected_host_is_rejected(mock_get):
    """SSRF guard: a media URL must actually be on a didit.me (sub)domain
    -- if Didit's response were ever manipulated (bug, MITM, compromised
    session), this stops the server from blindly fetching an
    attacker-controlled URL."""
    with pytest.raises(ValueError, match="unexpected host"):
        _assert_safe_media_url("https://evil.example.com/front.jpg")
    mock_get.assert_not_called()


def test_image_url_with_non_https_scheme_is_rejected():
    with pytest.raises(ValueError, match="non-https"):
        _assert_safe_media_url("http://cdn.didit.me/front.jpg")


@patch("apps.distributors.services.socket.gethostbyname", return_value="52.1.2.3")
def test_didits_real_s3_media_host_is_allowed(mock_dns):
    """Regression test: confirmed 2026-07-14 against a real live Didit
    verification session that Didit serves document/selfie images from
    this exact S3 bucket, not a didit.me (sub)domain -- the SSRF guard's
    original *.didit.me-only allowlist rejected every real image."""
    _assert_safe_media_url(
        "https://service-didit-verification-production-a1c5f9b8.s3.amazonaws.com/front.jpg"
    )


@patch("apps.distributors.services.socket.gethostbyname", return_value="52.1.2.3")
def test_a_lookalike_s3_bucket_is_still_rejected(mock_dns):
    """The allowlist is an exact host match, not a "contains didit"
    substring check -- an attacker-controlled bucket that merely mentions
    didit in its name must not pass."""
    with pytest.raises(ValueError, match="unexpected host"):
        _assert_safe_media_url(
            "https://evil-didit-verification-lookalike.s3.amazonaws.com/front.jpg"
        )


@patch("apps.distributors.services.socket.gethostbyname", return_value="127.0.0.1")
def test_image_url_resolving_to_a_private_ip_is_rejected(mock_dns):
    """Even a URL on an allowed hostname is rejected if that hostname
    actually resolves to a private/internal address (DNS rebinding /
    misconfiguration)."""
    with pytest.raises(ValueError, match="non-public IP"):
        _assert_safe_media_url("https://cdn.didit.me/front.jpg")


@pytest.mark.django_db
@patch("apps.distributors.services.requests.get")
@patch("apps.distributors.services.get_session_decision")
def test_image_from_an_unsafe_url_is_skipped_without_crashing(mock_decision, mock_get):
    verification = _make_verification()
    mock_decision.return_value = _approved_decision(
        id_verifications=[
            {"status": "Approved", "front_image": "https://evil.example.com/x.jpg"}
        ]
    )

    consume_didit_result("sess-1")  # must not raise

    verification.refresh_from_db()
    assert not verification.id_front_image
    mock_get.assert_not_called()

    verification.refresh_from_db()
    assert not verification.id_front_image
    mock_get.assert_not_called()
