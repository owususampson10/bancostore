import io
import os
from itertools import count

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_uploaded_image(name="card.jpg", size=(2000, 1600), color="blue", fmt="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


def _make_oversized_upload():
    # A real (small, cheap-to-encode) JPEG with junk bytes appended past its
    # end-of-image marker -- Pillow's Image.open()/verify() only reads the
    # actual image structure and tolerates trailing bytes, so this reliably
    # exceeds the 5MB cap without the cost of encoding megabytes of genuine
    # (near-incompressible) pixel data.
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(buffer, format="JPEG")
    padded = buffer.getvalue() + os.urandom(6 * 1024 * 1024)
    return SimpleUploadedFile("huge.jpg", padded, content_type="image/jpeg")


def _make_distributor(phone_verified=True):
    phone = f"+233243{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, phone_verified=phone_verified
    )


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_submit_kyc_requires_login(client):
    response = client.get(reverse("distributors:submit_kyc"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_phone_unverified_distributor_is_blocked_from_submitting_kyc(client):
    distributor = _make_distributor(phone_verified=False)
    _login(client, distributor)

    response = client.get(reverse("distributors:submit_kyc"))

    assert response.status_code == 200
    assert "Verify your phone number" in response.content.decode()
    distributor.refresh_from_db()
    assert distributor.kyc_submitted_at is None


@pytest.mark.django_db
def test_complete_submission_succeeds_and_stays_pending(client):
    distributor = _make_distributor(phone_verified=True)
    _login(client, distributor)

    response = client.post(
        reverse("distributors:submit_kyc"),
        {
            "ghana_card_front": _make_uploaded_image("front.jpg"),
            "ghana_card_back": _make_uploaded_image("back.jpg"),
            "selfie": _make_uploaded_image("selfie.jpg"),
        },
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.PENDING
    assert distributor.kyc_submitted_at is not None
    assert distributor.ghana_card_front.name.endswith(".webp")
    assert distributor.ghana_card_back.name.endswith(".webp")
    assert distributor.selfie.name.endswith(".webp")


@pytest.mark.django_db
def test_oversized_upload_is_rejected_with_a_clear_error(client):
    distributor = _make_distributor(phone_verified=True)
    _login(client, distributor)

    response = client.post(
        reverse("distributors:submit_kyc"),
        {
            "ghana_card_front": _make_oversized_upload(),
            "ghana_card_back": _make_uploaded_image("back.jpg"),
            "selfie": _make_uploaded_image("selfie.jpg"),
        },
    )

    assert response.status_code == 200
    assert "smaller than 5MB" in response.content.decode()
    distributor.refresh_from_db()
    assert distributor.kyc_submitted_at is None
    assert not distributor.ghana_card_front


@pytest.mark.django_db
def test_incomplete_submission_is_rejected_with_a_clear_error(client):
    distributor = _make_distributor(phone_verified=True)
    _login(client, distributor)

    response = client.post(
        reverse("distributors:submit_kyc"),
        {
            "ghana_card_front": _make_uploaded_image("front.jpg"),
            # ghana_card_back and selfie deliberately omitted
        },
    )

    assert response.status_code == 200
    assert "This field is required" in response.content.decode()
    distributor.refresh_from_db()
    assert distributor.kyc_submitted_at is None
    assert not distributor.ghana_card_front


@pytest.mark.django_db
def test_resubmission_overwrites_previous_submission_and_resets_to_pending(client):
    distributor = _make_distributor(phone_verified=True)
    distributor.kyc_status = Distributor.KycStatus.REJECTED
    distributor.ghana_card_front = _make_uploaded_image("old-front.jpg")
    distributor.save()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:submit_kyc"),
        {
            "ghana_card_front": _make_uploaded_image("new-front.jpg"),
            "ghana_card_back": _make_uploaded_image("new-back.jpg"),
            "selfie": _make_uploaded_image("new-selfie.jpg"),
        },
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.kyc_status == Distributor.KycStatus.PENDING
    assert "new-front" in distributor.ghana_card_front.name


@pytest.mark.django_db
def test_approved_distributor_cannot_resubmit(client):
    distributor = _make_distributor(phone_verified=True)
    distributor.kyc_status = Distributor.KycStatus.APPROVED
    distributor.save()
    _login(client, distributor)

    response = client.get(reverse("distributors:submit_kyc"))

    assert response.status_code == 302
    assert response.url == reverse("distributors:dashboard")
