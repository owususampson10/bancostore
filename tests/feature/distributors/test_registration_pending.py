import re

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor, PendingRegistration

User = get_user_model()

VALID_REGISTRATION_DATA = {
    "full_name": "Kofi Mensah",
    "phone_number": "+233241234567",
    "email": "kofi@example.test",
    "address": "12 Ring Road",
    "area": "Osu",
    "landmark": "Near the market",
    "password1": "S3cure-Passw0rd!",
    "password2": "S3cure-Passw0rd!",
    "terms_accepted": "on",
}


def _make_sponsor(ir_id="IR00001", phone_number="+233209999999"):
    user = User.objects.create_user(username=phone_number, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone_number, ir_id=ir_id)


@pytest.mark.django_db
def test_valid_registration_creates_a_pending_registration_not_an_account(client):
    sponsor = _make_sponsor()

    response = client.post(
        reverse("distributors:register"),
        {**VALID_REGISTRATION_DATA, "sponsor_ir_id": sponsor.ir_id},
    )

    assert response.status_code == 302
    assert not User.objects.filter(username="+233241234567").exists()
    assert not Distributor.objects.filter(phone_number="+233241234567").exists()

    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    assert pending.sponsor_id == sponsor.id
    assert pending.consumed_at is None
    assert pending.full_name == "Kofi Mensah"
    # The password must never be stored in plaintext anywhere, and the
    # stored hash must actually verify against the submitted password (not
    # just "doesn't contain the plaintext substring" -- that alone wouldn't
    # catch make_password being called with the wrong argument).
    assert "S3cure-Passw0rd!" not in pending.password_hash
    assert check_password("S3cure-Passw0rd!", pending.password_hash)


@pytest.mark.django_db
def test_registration_redirects_to_the_payment_step(client):
    sponsor = _make_sponsor()

    response = client.post(
        reverse("distributors:register"),
        {**VALID_REGISTRATION_DATA, "sponsor_ir_id": sponsor.ir_id},
    )

    assert response.url == reverse("distributors:pay_registration_fee")


@pytest.mark.django_db
def test_registration_stores_the_pending_registration_token_in_session(client):
    sponsor = _make_sponsor()

    client.post(
        reverse("distributors:register"),
        {**VALID_REGISTRATION_DATA, "sponsor_ir_id": sponsor.ir_id},
    )

    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    assert client.session["pending_registration_token"] == str(pending.token)


@pytest.mark.django_db
def test_registration_rejects_a_nonexistent_sponsor_ir_id(client):
    response = client.post(
        reverse("distributors:register"),
        {**VALID_REGISTRATION_DATA, "sponsor_ir_id": "IR99999"},
    )

    assert response.status_code == 200
    assert not PendingRegistration.objects.exists()
    assert "sponsor_ir_id" in response.context["form"].errors


@pytest.mark.django_db
def test_referral_link_prefills_the_sponsor_field(client):
    sponsor = _make_sponsor()

    response = client.get(reverse("distributors:register"), {"ref": sponsor.ir_id})

    assert response.status_code == 200
    assert response.context["form"].initial.get("sponsor_ir_id") == sponsor.ir_id


@pytest.mark.django_db
def test_duplicate_pending_registration_for_the_same_phone_is_rejected(client):
    sponsor = _make_sponsor()
    data = {**VALID_REGISTRATION_DATA, "sponsor_ir_id": sponsor.ir_id}
    client.post(reverse("distributors:register"), data)

    second_response = client.post(reverse("distributors:register"), data)

    assert second_response.status_code == 200
    assert PendingRegistration.objects.filter(phone_number="+233241234567").count() == 1


@pytest.mark.django_db
def test_registration_does_not_check_the_distributor_table_for_this_phone(client):
    """Enumeration prevention, confirmed via doubt-driven-development: the
    form does not check the submitted phone/email against existing
    Distributor rows at submission time. The real uniqueness check happens
    later, at account-creation time in Task 10b's webhook handler -- not in
    a synchronous, probeable response here."""
    existing_user = User.objects.create_user(username="+233241234567", password="x")
    Distributor.objects.create(user=existing_user, phone_number="+233241234567")
    sponsor = _make_sponsor()

    response = client.post(
        reverse("distributors:register"),
        {**VALID_REGISTRATION_DATA, "sponsor_ir_id": sponsor.ir_id},
    )

    assert response.status_code == 302
    assert PendingRegistration.objects.filter(phone_number="+233241234567").exists()


@pytest.mark.django_db
def test_registration_is_rate_limited_per_ip(client):
    sponsor = _make_sponsor()
    responses = []
    for i in range(10):
        responses.append(
            client.post(
                reverse("distributors:register"),
                {
                    **VALID_REGISTRATION_DATA,
                    "phone_number": f"+23355002{i:04d}",
                    "email": f"dist{i}@example.test",
                    "sponsor_ir_id": sponsor.ir_id,
                },
            )
        )

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_registration_get_is_rate_limited_per_ip(client):
    """The sponsor-lock feature's GET-time Distributor.objects.filter(ir_id=
    ref).exists() lookup runs off attacker-controlled ?ref= on every GET --
    without a limit here, someone could enumerate real IR IDs (via whether
    the response renders locked) or hammer the DB, neither of which the
    pre-existing POST-only limit covers."""
    sponsor = _make_sponsor()
    responses = [
        client.get(reverse("distributors:register"), {"ref": sponsor.ir_id})
        for _ in range(25)
    ]

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_an_unknown_ref_value_does_not_crash_the_registration_page(client):
    """An invalid/unknown ?ref= value must not 500 -- it prefills the text
    field with the literal (auto-escaped) value, exactly like a distributor
    who typed a wrong IR ID by hand; clean_sponsor_ir_id's own
    ValidationError only fires on submit, not on this GET render."""
    response = client.get(reverse("distributors:register"), {"ref": "not-a-real-ir-id"})

    assert response.status_code == 200
    assert 'value="not-a-real-ir-id"' in response.content.decode()


@pytest.mark.django_db
def test_no_ref_query_param_leaves_the_sponsor_field_blank(client):
    response = client.get(reverse("distributors:register"))

    assert response.status_code == 200
    content = response.content.decode()
    # CodeRabbit: a fixed 100-char backward offset assumes the opening
    # <input is within that window -- fragile against markup changes
    # (extra attrs/classes before name=). A regex captures the whole tag
    # regardless of what precedes name= within it.
    match = re.search(r'<input[^>]*name="sponsor_ir_id"[^>]*>', content)
    assert match is not None
    sponsor_field_tag = match.group(0)
    # Django's Input.format_value() treats an explicitly-empty initial
    # value the same as no value at all -- no `value=` attribute renders,
    # distinguishing "no referral link used" from a broken/empty one.
    assert "value=" not in sponsor_field_tag
    assert "readonly" not in sponsor_field_tag


def _sponsor_field_tag(content):
    match = re.search(r'<input[^>]*name="sponsor_ir_id"[^>]*>', content)
    assert match is not None, "sponsor_ir_id field not found in response"
    return match.group(0)


@pytest.mark.django_db
def test_referral_link_with_a_real_sponsor_locks_the_field(client):
    """The bug report: a distributor's referral link pre-fills the sponsor
    field, but it was a plain editable text input the visitor could clear or
    overwrite before submitting. A real, existing sponsor's IR ID now
    renders the field readonly with a lock icon and a hidden field carrying
    the lock state through to the POST."""
    sponsor = _make_sponsor()

    response = client.get(reverse("distributors:register"), {"ref": sponsor.ir_id})

    content = response.content.decode()
    assert "readonly" in _sponsor_field_tag(content)
    assert response.context["sponsor_locked"] is True
    assert '<input type="hidden" name="sponsor_locked" value="1">' in content
    assert "Set from your referral link and can't be changed." in content


@pytest.mark.django_db
def test_referral_link_with_an_unknown_sponsor_does_not_lock_the_field(client):
    """A broken/typo'd referral link must stay editable so the visitor can
    correct it themselves -- only a real, existing sponsor locks the field."""
    response = client.get(reverse("distributors:register"), {"ref": "not-a-real-ir-id"})

    content = response.content.decode()
    assert "readonly" not in _sponsor_field_tag(content)
    assert response.context["sponsor_locked"] is False
    assert 'name="sponsor_locked"' not in content


@pytest.mark.django_db
def test_locked_sponsor_field_still_submits_and_registers(client):
    """readonly (unlike disabled) still submits its value -- a real browser
    posts the field's content even though the visitor couldn't edit it."""
    sponsor = _make_sponsor()

    response = client.post(
        reverse("distributors:register"),
        {
            **VALID_REGISTRATION_DATA,
            "sponsor_ir_id": sponsor.ir_id,
            "sponsor_locked": "1",
        },
    )

    assert response.status_code == 302
    pending = PendingRegistration.objects.get(phone_number="+233241234567")
    assert pending.sponsor_id == sponsor.id


@pytest.mark.django_db
def test_lock_state_survives_a_failed_post_rerender(client):
    """If some other field fails validation, the redisplayed form must keep
    the sponsor field locked -- otherwise the visitor could edit and clear
    it on the retry even though it arrived via a real referral link."""
    sponsor = _make_sponsor()

    response = client.post(
        reverse("distributors:register"),
        {
            **VALID_REGISTRATION_DATA,
            "sponsor_ir_id": sponsor.ir_id,
            "sponsor_locked": "1",
            "terms_accepted": "",  # fails validation, form redisplays
        },
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "readonly" in _sponsor_field_tag(content)
    assert response.context["sponsor_locked"] is True
