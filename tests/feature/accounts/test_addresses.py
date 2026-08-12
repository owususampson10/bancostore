from django.contrib.auth import get_user_model
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
