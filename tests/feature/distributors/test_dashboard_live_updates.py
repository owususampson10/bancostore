from decimal import Decimal
from itertools import count

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group

from apps.distributors.consumers import WalletBalanceConsumer
from apps.distributors.models import Distributor
from apps.wallet.services import credit

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    """Mirrors test_dashboard.py's own helper."""
    phone = f"+233249{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_customer(**overrides):
    phone = f"+233249{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!", **overrides)


async def _connect_as(user):
    communicator = WebsocketCommunicator(
        WalletBalanceConsumer.as_asgi(), "/ws/distributors/wallet/"
    )
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    return communicator, connected


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_connection_is_rejected():
    async def run():
        communicator, connected = await _connect_as(AnonymousUser())
        assert connected is False
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_non_distributor_authenticated_user_is_rejected():
    customer = _make_customer()

    async def run():
        communicator, connected = await _connect_as(customer)
        assert connected is False
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_distributor_connection_is_accepted_and_receives_initial_balance():
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("25.00"),
        transaction_type="direct_referral_bonus",
        reference="test-ref-1",
    )

    async def run():
        communicator, connected = await _connect_as(distributor.user)
        assert connected is True
        response = await communicator.receive_json_from()
        assert response == {"type": "wallet_balance_update", "balance": "25.00"}
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_distributor_with_no_wallet_yet_gets_zero_on_connect():
    distributor = _make_distributor()

    async def run():
        communicator, connected = await _connect_as(distributor.user)
        assert connected is True
        response = await communicator.receive_json_from()
        assert response == {"type": "wallet_balance_update", "balance": "0.00"}
        await communicator.disconnect()

    async_to_sync(run)()


@pytest.mark.django_db(transaction=True)
def test_a_credit_after_connecting_pushes_a_live_update_with_the_new_balance():
    distributor = _make_distributor()

    async def run():
        communicator, connected = await _connect_as(distributor.user)
        assert connected is True
        # Drain the initial zero-balance message sent on connect.
        await communicator.receive_json_from()

        await database_sync_to_async_credit(distributor)

        response = await communicator.receive_json_from()
        assert response == {"type": "wallet_balance_update", "balance": "40.00"}
        await communicator.disconnect()

    async_to_sync(run)()


async def database_sync_to_async_credit(distributor):
    from channels.db import database_sync_to_async

    await database_sync_to_async(credit)(
        distributor,
        Decimal("40.00"),
        transaction_type="direct_referral_bonus",
        reference="test-ref-2",
    )


@pytest.mark.django_db(transaction=True)
def test_a_distributor_never_receives_another_distributors_wallet_update():
    distributor_a = _make_distributor()
    distributor_b = _make_distributor()

    async def run():
        communicator_a, connected_a = await _connect_as(distributor_a.user)
        assert connected_a is True
        await communicator_a.receive_json_from()  # drain initial zero balance

        communicator_b, connected_b = await _connect_as(distributor_b.user)
        assert connected_b is True
        await communicator_b.receive_json_from()  # drain initial zero balance

        await database_sync_to_async_credit(distributor_a)

        # distributor_a's socket gets the update...
        response_a = await communicator_a.receive_json_from()
        assert response_a == {"type": "wallet_balance_update", "balance": "40.00"}

        # ...but distributor_b's socket must receive nothing at all.
        assert await communicator_b.receive_nothing(timeout=0.5) is True

        await communicator_a.disconnect()
        await communicator_b.disconnect()

    async_to_sync(run)()
