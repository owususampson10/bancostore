from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest

from apps.distributors.models import Distributor, PendingRegistration
from apps.distributors.tasks import cleanup_expired_pending_registrations

User = get_user_model()


def _make_sponsor(ir_id="IR00001", phone_number="+233209999999"):
    user = User.objects.create_user(username=phone_number, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone_number, ir_id=ir_id)


def _make_pending(sponsor, phone_number):
    return PendingRegistration.objects.create(
        full_name="Test Person",
        phone_number=phone_number,
        email="person@example.test",
        address="1 Test Street",
        area="Osu",
        landmark="",
        password_hash="hashed",
        sponsor=sponsor,
    )


@pytest.mark.django_db
def test_cleanup_deletes_unconsumed_registrations_older_than_one_hour():
    sponsor = _make_sponsor()
    stale = _make_pending(sponsor, "+233241111111")
    PendingRegistration.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - timedelta(hours=2)
    )
    fresh = _make_pending(sponsor, "+233241111112")

    cleanup_expired_pending_registrations()

    assert not PendingRegistration.objects.filter(pk=stale.pk).exists()
    assert PendingRegistration.objects.filter(pk=fresh.pk).exists()


@pytest.mark.django_db
def test_cleanup_keeps_registrations_within_the_one_hour_window():
    sponsor = _make_sponsor()
    almost_stale = _make_pending(sponsor, "+233241111113")
    PendingRegistration.objects.filter(pk=almost_stale.pk).update(
        created_at=timezone.now() - timedelta(minutes=59)
    )

    cleanup_expired_pending_registrations()

    assert PendingRegistration.objects.filter(pk=almost_stale.pk).exists()
