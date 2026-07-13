from django.contrib.auth import get_user_model
from django.db import IntegrityError

import pytest

from apps.distributors.models import Distributor, PendingRegistration

User = get_user_model()


def _make_sponsor(ir_id="IR00001", phone_number="+233209999999"):
    user = User.objects.create_user(username=phone_number, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone_number, ir_id=ir_id)


def _make_pending(sponsor, phone_number="+233241111111", password_hash="hashed"):
    return PendingRegistration.objects.create(
        full_name="Test Person",
        phone_number=phone_number,
        email="person@example.test",
        address="1 Test Street",
        area="Osu",
        landmark="",
        password_hash=password_hash,
        sponsor=sponsor,
    )


@pytest.mark.django_db
def test_pending_registration_requires_a_unique_phone_number():
    sponsor = _make_sponsor()
    _make_pending(sponsor, phone_number="+233241111111")

    with pytest.raises(IntegrityError):
        _make_pending(sponsor, phone_number="+233241111111")


@pytest.mark.django_db
def test_pending_registration_gets_a_unique_token_automatically():
    sponsor = _make_sponsor()
    first = _make_pending(sponsor, phone_number="+233241111111")
    second = _make_pending(sponsor, phone_number="+233241111112")

    assert first.token is not None
    assert first.token != second.token


@pytest.mark.django_db
def test_pending_registration_defaults_to_unconsumed():
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)

    assert pending.consumed_at is None
