from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.accounts.models import Address

User = get_user_model()


def _address_payload(**overrides):
    payload = {
        "label": "Home",
        "delivery_zone": "kumasi",
        "address": "12 Ridge Road",
        "area": "Ridge",
        "landmark": "Near the market",
        "is_default": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_address_list_requires_login(client):
    response = client.get(reverse("accounts:address_list"))

    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


@pytest.mark.django_db
def test_address_list_shows_only_the_logged_in_users_own_addresses(client):
    owner = User.objects.create_user(username="ama@example.test", password="pw")
    other = User.objects.create_user(username="kofi@example.test", password="pw")
    Address.objects.create(user=owner, address="Mine Street", delivery_zone="kumasi")
    Address.objects.create(user=other, address="Theirs Street", delivery_zone="accra")

    client.force_login(owner)
    response = client.get(reverse("accounts:address_list"))

    content = response.content.decode()
    assert "Mine Street" in content
    assert "Theirs Street" not in content


@pytest.mark.django_db
def test_logged_in_user_can_create_an_address(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.post(reverse("accounts:address_create"), _address_payload())

    assert response.status_code == 302
    address = Address.objects.get(user=user)
    assert address.address == "12 Ridge Road"
    assert address.delivery_zone == "kumasi"


@pytest.mark.django_db
def test_logged_in_user_can_edit_their_address(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    address = Address.objects.create(
        user=user, address="Old Street", delivery_zone="kumasi"
    )
    client.force_login(user)

    response = client.post(
        reverse("accounts:address_edit", args=[address.pk]),
        _address_payload(address="New Street"),
    )

    assert response.status_code == 302
    address.refresh_from_db()
    assert address.address == "New Street"


@pytest.mark.django_db
def test_logged_in_user_can_delete_their_address(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    address = Address.objects.create(
        user=user, address="Delete Me", delivery_zone="kumasi"
    )
    client.force_login(user)

    response = client.post(reverse("accounts:address_delete", args=[address.pk]))

    assert response.status_code == 302
    assert not Address.objects.filter(pk=address.pk).exists()


@pytest.mark.django_db
def test_a_user_cannot_edit_another_users_address(client):
    owner = User.objects.create_user(username="ama@example.test", password="pw")
    attacker = User.objects.create_user(username="kofi@example.test", password="pw")
    address = Address.objects.create(
        user=owner, address="Owner Street", delivery_zone="kumasi"
    )
    client.force_login(attacker)

    response = client.post(
        reverse("accounts:address_edit", args=[address.pk]),
        _address_payload(address="Hacked Street"),
    )

    assert response.status_code == 404
    address.refresh_from_db()
    assert address.address == "Owner Street"


@pytest.mark.django_db
def test_a_user_cannot_delete_another_users_address(client):
    owner = User.objects.create_user(username="ama@example.test", password="pw")
    attacker = User.objects.create_user(username="kofi@example.test", password="pw")
    address = Address.objects.create(
        user=owner, address="Owner Street", delivery_zone="kumasi"
    )
    client.force_login(attacker)

    response = client.post(reverse("accounts:address_delete", args=[address.pk]))

    assert response.status_code == 404
    assert Address.objects.filter(pk=address.pk).exists()


@pytest.mark.django_db
def test_marking_a_second_address_default_unsets_the_first():
    """The model's own save() enforcement, not a view-level concern --
    tested directly against the model since this invariant must hold no
    matter which code path saves an Address."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    first = Address.objects.create(
        user=user, address="First", delivery_zone="kumasi", is_default=True
    )
    second = Address.objects.create(
        user=user, address="Second", delivery_zone="accra", is_default=True
    )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_default is False
    assert second.is_default is True


@pytest.mark.django_db
def test_default_address_is_not_required():
    user = User.objects.create_user(username="ama@example.test", password="pw")

    address = Address.objects.create(
        user=user, address="No Default", delivery_zone="kumasi"
    )

    assert address.is_default is False


@pytest.mark.django_db
def test_setting_a_new_default_locks_the_existing_default_row_first():
    """code-review-and-quality finding (CodeRabbit, then a security-auditor
    pass on the first fix attempt, then a CodeRabbit follow-up on the
    second): a bare exclude().update() with no locking, then a
    select_for_update() chained directly before .update() (a genuine no-op
    in Django -- .update() compiles to a bulk UPDATE and never evaluates
    the queryset), both failed to actually acquire a row lock before
    clearing the old default. An initial regression test only asserted
    that select_for_update() was *called* -- CodeRabbit correctly pointed
    out that proves nothing, since the exact bug being fixed was that the
    call happens without the queryset ever being *evaluated*. Rewritten to
    capture the real SQL instead: the fixed code issues a genuine SELECT
    (the locking fetch, forced via list()) before the UPDATE that clears
    the old default; the buggy chained-onto-.update() version issues no
    such SELECT at all. Works identically on SQLite (locally) and MySQL
    (CI) since it only checks statement order/shape, not FOR UPDATE
    literal text (SQLite silently drops that clause -- it doesn't support
    row locking at all, matching bancostore/concurrency.py's own
    documented SQLite-vs-MySQL distinction). This still doesn't prove the
    race is closed under real concurrency (a full multi-threaded test is
    disproportionate for this low-stakes, non-money feature per
    SPEC_PHASE2.md's own scoping), but it proves the locking code path
    genuinely executes as written, not just that a comment claims it
    does."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    Address.objects.create(
        user=user, address="First", delivery_zone="kumasi", is_default=True
    )

    with CaptureQueriesContext(connection) as ctx:
        Address.objects.create(
            user=user, address="Second", delivery_zone="accra", is_default=True
        )

    statements = [q["sql"].strip().upper() for q in ctx.captured_queries]
    select_index = next(
        (i for i, sql in enumerate(statements) if sql.startswith("SELECT")), None
    )
    update_index = next(
        (i for i, sql in enumerate(statements) if sql.startswith("UPDATE")), None
    )
    assert select_index is not None, (
        "expected a real SELECT (the locking fetch) while setting a new "
        "default -- none was issued, meaning select_for_update() was never "
        "actually evaluated. Queries: " + "; ".join(statements)
    )
    assert update_index is not None, "expected the default-clearing UPDATE to run"
    assert select_index < update_index, (
        "expected the locking SELECT to run before the UPDATE that clears "
        "the old default, not after -- got: " + "; ".join(statements)
    )
