from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.notifications.models import Notification

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    """Mirrors test_earnings_history.py's own helper -- every real
    distributor is added to the "distributor" group by
    consume_paid_registration, so tests must match that shape."""
    phone = f"+233255{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


def _make_notification(distributor, **overrides):
    defaults = {
        "distributor": distributor,
        "event_type": Notification.EventType.KYC_DECIDED,
        "message": "test notification",
        "is_read": False,
    }
    defaults.update(overrides)
    return Notification.objects.create(**defaults)


# --- notification_dropdown ---------------------------------------------


@pytest.mark.django_db
def test_dropdown_requires_login(client):
    response = client.get(reverse("distributors:notification_dropdown"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash_on_dropdown(client):
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:notification_dropdown"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_dropdown_lists_own_notifications_newest_first(client):
    distributor = _make_distributor()
    _login(client, distributor)
    _make_notification(distributor, message="older")
    _make_notification(distributor, message="newer")

    response = client.get(reverse("distributors:notification_dropdown"))

    assert response.status_code == 200
    content = response.content.decode()
    assert content.index("newer") < content.index("older")


@pytest.mark.django_db
def test_dropdown_never_drops_an_unread_item_for_a_burst_of_newer_read_items(client):
    """Unread items must never be pushed off the 10-item dropdown purely
    by a burst of newer already-read items -- otherwise the header badge
    would show a nonzero unread count with nothing actionable visible in
    the dropdown itself (only reachable via "View all")."""
    distributor = _make_distributor()
    _login(client, distributor)
    _make_notification(distributor, message="oldest but unread", is_read=False)
    for i in range(10):
        _make_notification(distributor, message=f"newer read {i}", is_read=True)

    response = client.get(reverse("distributors:notification_dropdown"))

    assert "oldest but unread" in response.content.decode()


@pytest.mark.django_db
def test_dropdown_never_shows_another_distributors_notifications(client):
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()
    _login(client, distributor_b)
    _make_notification(distributor_a, message="belongs to A only")

    response = client.get(reverse("distributors:notification_dropdown"))

    assert "belongs to A only" not in response.content.decode()


@pytest.mark.django_db
def test_dropdown_get_never_marks_anything_read(client):
    distributor = _make_distributor()
    _login(client, distributor)
    notification = _make_notification(distributor, is_read=False)

    client.get(reverse("distributors:notification_dropdown"))

    notification.refresh_from_db()
    assert notification.is_read is False


# --- notification_mark_read ---------------------------------------------


@pytest.mark.django_db
def test_mark_read_requires_post(client):
    distributor = _make_distributor()
    _login(client, distributor)
    notification = _make_notification(distributor)

    response = client.get(
        reverse("distributors:notification_mark_read", args=[notification.pk])
    )

    assert response.status_code == 405


@pytest.mark.django_db
def test_mark_read_marks_the_notification_read(client):
    distributor = _make_distributor()
    _login(client, distributor)
    notification = _make_notification(distributor, is_read=False)

    response = client.post(
        reverse("distributors:notification_mark_read", args=[notification.pk])
    )

    assert response.status_code == 200
    notification.refresh_from_db()
    assert notification.is_read is True


@pytest.mark.django_db
def test_mark_read_is_idor_safe_against_another_distributors_notification(client):
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()
    _login(client, distributor_b)
    notification = _make_notification(distributor_a, is_read=False)

    response = client.post(
        reverse("distributors:notification_mark_read", args=[notification.pk])
    )

    assert response.status_code == 404
    notification.refresh_from_db()
    assert notification.is_read is False


# --- notification_mark_all_read -----------------------------------------


@pytest.mark.django_db
def test_mark_all_read_requires_post(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:notification_mark_all_read"))

    assert response.status_code == 405


@pytest.mark.django_db
def test_mark_all_read_marks_every_unread_notification_for_that_distributor(client):
    distributor = _make_distributor()
    _login(client, distributor)
    first = _make_notification(distributor, is_read=False)
    second = _make_notification(distributor, is_read=False)

    response = client.post(reverse("distributors:notification_mark_all_read"))

    assert response.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_read is True
    assert second.is_read is True


@pytest.mark.django_db
def test_mark_all_read_never_touches_another_distributors_notifications(client):
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()
    _login(client, distributor_b)
    other_notification = _make_notification(distributor_a, is_read=False)

    client.post(reverse("distributors:notification_mark_all_read"))

    other_notification.refresh_from_db()
    assert other_notification.is_read is False


@pytest.mark.django_db
def test_mark_all_read_reports_the_true_current_count_not_an_assumed_zero(
    client, monkeypatch
):
    """Regression guard (CodeRabbit, PR #51): the response body must
    reflect the actual current unread count, not a hard-coded 0 -- a
    notification created in the narrow window between the mark-all-read
    update() and this response being rendered (e.g. a concurrent
    Celery-driven bonus credit for this same distributor) must still be
    reflected, rather than silently under-reporting until the next
    fetch or WebSocket push happens to correct it."""
    distributor = _make_distributor()
    _login(client, distributor)
    _make_notification(distributor, is_read=False)

    def _race_in_a_new_notification(*args, **kwargs):
        _make_notification(distributor, message="raced in mid-request", is_read=False)

    monkeypatch.setattr(
        "apps.distributors.views.push_unread_count_update",
        _race_in_a_new_notification,
    )

    response = client.post(reverse("distributors:notification_mark_all_read"))

    assert response.context["unread_notification_count"] == 1


# --- notification_history ------------------------------------------------


@pytest.mark.django_db
def test_history_requires_login(client):
    response = client.get(reverse("distributors:notification_history"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_history_lists_own_notifications(client):
    distributor = _make_distributor()
    _login(client, distributor)
    _make_notification(distributor, message="my history item")

    response = client.get(reverse("distributors:notification_history"))

    assert response.status_code == 200
    assert "my history item" in response.content.decode()


@pytest.mark.django_db
def test_history_never_shows_another_distributors_notifications(client):
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()
    _login(client, distributor_b)
    _make_notification(distributor_a, message="belongs to A only")

    response = client.get(reverse("distributors:notification_history"))

    assert "belongs to A only" not in response.content.decode()


@pytest.mark.django_db
def test_history_paginates_at_20(client):
    distributor = _make_distributor()
    _login(client, distributor)
    for i in range(25):
        _make_notification(distributor, message=f"item {i}")

    response = client.get(reverse("distributors:notification_history"))

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 25
    assert len(response.context["page_obj"].object_list) == 20
