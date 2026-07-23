from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import DiditVerification, Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(
    full_name="",
    ir_id=None,
    rank="",
    kyc_status=Distributor.KycStatus.PENDING,
    sponsor=None,
    is_active=True,
):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_active=is_active
    )
    return Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name=full_name,
        ir_id=ir_id,
        rank=rank,
        kyc_status=kyc_status,
        sponsor=sponsor,
    )


def _directory_url():
    return reverse("admin_portal:distributor_directory")


def _profile_url(distributor):
    return reverse("admin_portal:distributor_profile", args=[distributor.pk])


@pytest.mark.django_db
def test_directory_lists_distributors(staff_client):
    _make_distributor(full_name="Kofi Owusu")

    response = staff_client.get(_directory_url())

    assert response.status_code == 200
    assert b"Kofi Owusu" in response.content


@pytest.mark.django_db
def test_directory_search_matches_by_name(staff_client):
    _make_distributor(full_name="Ama Mensah")
    _make_distributor(full_name="Kojo Antwi")

    response = staff_client.get(_directory_url(), {"q": "Ama"})

    body = response.content.decode()
    assert "Ama Mensah" in body
    assert "Kojo Antwi" not in body


@pytest.mark.django_db
def test_directory_search_matches_by_ir_id(staff_client):
    _make_distributor(full_name="Ama Mensah", ir_id="IR00042")
    _make_distributor(full_name="Kojo Antwi", ir_id="IR00099")

    response = staff_client.get(_directory_url(), {"q": "IR00042"})

    body = response.content.decode()
    assert "Ama Mensah" in body
    assert "Kojo Antwi" not in body


@pytest.mark.django_db
def test_directory_search_matches_by_phone(staff_client):
    distributor = _make_distributor(full_name="Ama Mensah")
    _make_distributor(full_name="Kojo Antwi")

    response = staff_client.get(_directory_url(), {"q": str(distributor.phone_number)})

    body = response.content.decode()
    assert "Ama Mensah" in body
    assert "Kojo Antwi" not in body


@pytest.mark.django_db
def test_directory_shows_an_empty_state_for_no_search_matches(staff_client):
    _make_distributor(full_name="Ama Mensah")

    response = staff_client.get(_directory_url(), {"q": "no-such-distributor"})

    assert response.status_code == 200
    assert b"No distributors match your search." in response.content


@pytest.mark.django_db
def test_directory_paginates_at_twenty_per_page(staff_client):
    for i in range(25):
        _make_distributor(full_name=f"Distributor {i:02d}")

    first_page = staff_client.get(_directory_url())
    second_page = staff_client.get(_directory_url(), {"page": 2})

    assert b"Distributor 00" in first_page.content
    assert b"Distributor 00" not in second_page.content
    assert b"Page 1 of 2" in first_page.content


@pytest.mark.django_db
def test_directory_shows_account_status_pills(staff_client):
    _make_distributor(full_name="Active One", is_active=True)
    _make_distributor(full_name="Suspended One", is_active=False)

    response = staff_client.get(_directory_url())

    body = response.content.decode()
    assert "Active" in body
    assert "Suspended" in body


@pytest.mark.django_db
def test_profile_shows_distributor_details(staff_client):
    sponsor = _make_distributor(full_name="Sponsor Name", ir_id="IR00001")
    distributor = _make_distributor(
        full_name="Kwame Asante",
        ir_id="IR00042",
        rank="Bronze",
        kyc_status=Distributor.KycStatus.APPROVED,
        sponsor=sponsor,
    )

    response = staff_client.get(_profile_url(distributor))

    body = response.content.decode()
    assert response.status_code == 200
    assert "Kwame Asante" in body
    assert "IR00042" in body
    assert "Bronze" in body
    assert "Sponsor Name" in body


@pytest.mark.django_db
def test_profile_shows_no_sponsor_for_a_root_distributor(staff_client):
    distributor = _make_distributor(full_name="Root Distributor", sponsor=None)

    response = staff_client.get(_profile_url(distributor))

    assert b"No sponsor" in response.content


@pytest.mark.django_db
def test_profile_shows_not_yet_purchased_when_no_starter_pack(staff_client):
    distributor = _make_distributor(full_name="No Pack Yet")

    response = staff_client.get(_profile_url(distributor))

    assert b"Not yet purchased" in response.content


@pytest.mark.django_db
def test_profile_shows_pack_label_from_the_real_starter_pack_choice(staff_client):
    distributor = _make_distributor(full_name="Has A Pack")
    distributor.starter_pack_choice = "A"
    distributor.save(update_fields=["starter_pack_choice"])

    response = staff_client.get(_profile_url(distributor))

    body = response.content.decode()
    assert "Pack A" in body
    assert "Premium Wellness Pack" not in body  # the fabricated Stitch mockup name


@pytest.mark.django_db
def test_profile_falls_back_to_the_extracted_id_name_when_full_name_is_blank(
    staff_client,
):
    """Mirrors the identical fallback already proven for the KYC review and
    withdrawal review detail screens: a distributor with no full_name set
    still has a real name sitting in their DiditVerification, and every
    surface here (title, header, and the suspend/reactivate flash message
    and modal copy) must use it instead of falling back to
    Distributor.__str__'s "Distributor<+233...>" debug repr."""
    distributor = _make_distributor(full_name="")
    DiditVerification.objects.create(
        distributor=distributor,
        session_id=f"sess-{distributor.pk}",
        extracted_full_name="Kojo Antwi",
    )

    response = staff_client.get(_profile_url(distributor))

    body = response.content.decode()
    assert "Kojo Antwi" in body
    assert "Distributor<" not in body


@pytest.mark.django_db
def test_suspend_flash_message_uses_the_real_name_not_the_debug_repr(staff_client):
    distributor = _make_distributor(full_name="")
    DiditVerification.objects.create(
        distributor=distributor,
        session_id=f"sess-{distributor.pk}",
        extracted_full_name="Kojo Antwi",
    )

    response = staff_client.post(
        _profile_url(distributor), {"action": "toggle_active"}, follow=True
    )

    body = response.content.decode()
    assert "Suspended Kojo Antwi" in body
    assert "Distributor<" not in body


@pytest.mark.django_db
def test_directory_pagination_is_stable_when_full_names_collide(staff_client):
    """Task 15's earnings-history pagination had the identical bug: sorting
    by a non-unique column with no tie-breaker can skip or duplicate a row
    across a page boundary. Every distributor here shares the same
    full_name, so pk must be the tie-breaker or page 2 will repeat rows
    from page 1 instead of showing the next 5."""
    distributors = [_make_distributor(full_name="Same Name") for _ in range(25)]

    first_page = staff_client.get(_directory_url())
    second_page = staff_client.get(_directory_url(), {"page": 2})

    first_page_ir_ids = {d.pk for d in distributors[:20]}
    second_page_ir_ids = {d.pk for d in distributors[20:]}
    first_body = first_page.content.decode()
    second_body = second_page.content.decode()
    for pk in first_page_ir_ids:
        assert f'/distributors/{pk}/"' in first_body
    for pk in second_page_ir_ids:
        assert f'/distributors/{pk}/"' in second_body
        assert f'/distributors/{pk}/"' not in first_body


@pytest.mark.django_db
def test_suspending_an_active_distributor_blocks_login(staff_client):
    distributor = _make_distributor(full_name="Kofi Owusu", is_active=True)

    response = staff_client.post(
        _profile_url(distributor), {"action": "toggle_active"}, follow=True
    )

    assert response.status_code == 200
    distributor.user.refresh_from_db()
    assert distributor.user.is_active is False
    assert b"Suspended Kofi Owusu" in response.content


@pytest.mark.django_db
def test_reactivating_a_suspended_distributor_restores_login(staff_client):
    distributor = _make_distributor(full_name="Kofi Owusu", is_active=False)

    response = staff_client.post(
        _profile_url(distributor), {"action": "toggle_active"}, follow=True
    )

    assert response.status_code == 200
    distributor.user.refresh_from_db()
    assert distributor.user.is_active is True
    assert b"Reactivated Kofi Owusu" in response.content


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden_from_the_directory(client, db):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_directory_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_directory_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url
