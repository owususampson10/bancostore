from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    """Mirrors test_dashboard.py's own helper."""
    phone = f"+233250{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_customer(**overrides):
    phone = f"+233250{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!", **overrides)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_binary_tree_view_requires_login(client):
    response = client.get(reverse("distributors:binary_tree"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    customer = _make_customer()
    client.force_login(customer)

    response = client.get(reverse("distributors:binary_tree"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_a_distributor_with_no_downline_sees_an_honest_empty_state(client):
    distributor = _make_distributor(full_name="Solo Distributor", ir_id="IR00100")
    _login(client, distributor)

    response = client.get(reverse("distributors:binary_tree"))

    assert response.status_code == 200
    assert response.context["tree"].children == []
    assert response.context["tree"].full_name == "Solo Distributor"


@pytest.mark.django_db
def test_downline_renders_with_correct_names_and_ir_ids(client):
    root = _make_distributor(full_name="Root Distributor", ir_id="IR00101")
    left_child = _make_distributor(full_name="Left Kid", ir_id="IR00102")
    BinaryTree.place_distributor(root, left_child, leg=BinaryTreeEdge.Leg.LEFT)
    _login(client, root)

    response = client.get(reverse("distributors:binary_tree"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Left Kid" in content
    assert "IR00102" in content


@pytest.mark.django_db
def test_a_distributor_never_sees_another_distributors_tree(client):
    """No id/param exists to manipulate -- always scoped to
    request.user.distributor, same IDOR-safe-by-construction pattern as
    earnings_history."""
    distributor_a = _make_distributor(full_name="Distributor A", ir_id="IR00103")
    distributor_b = _make_distributor(full_name="Distributor B", ir_id="IR00104")
    child_of_b = _make_distributor(full_name="Child Of B", ir_id="IR00105")
    BinaryTree.place_distributor(distributor_b, child_of_b, leg=BinaryTreeEdge.Leg.LEFT)
    _login(client, distributor_a)

    response = client.get(reverse("distributors:binary_tree"))

    assert response.status_code == 200
    assert "Child Of B" not in response.content.decode()
    assert response.context["tree"].distributor_id == distributor_a.pk
