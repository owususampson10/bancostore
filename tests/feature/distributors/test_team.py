from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    """Mirrors test_binary_tree_view.py's own helper."""
    phone = f"+233251{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_customer(**overrides):
    phone = f"+233251{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!", **overrides)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_team_requires_login(client):
    response = client.get(reverse("distributors:team"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    customer = _make_customer()
    client.force_login(customer)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_a_distributor_with_no_recruits_sees_an_honest_empty_state(client):
    distributor = _make_distributor(full_name="Solo Distributor", ir_id="IR00200")
    _login(client, distributor)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 0
    assert b"No team members yet" in response.content


@pytest.mark.django_db
def test_team_roster_shows_direct_and_indirect_recruits(client):
    root = _make_distributor(full_name="Root Distributor", ir_id="IR00201")
    direct = _make_distributor(
        full_name="Direct Recruit",
        ir_id="IR00202",
        rank="bronze",
        kyc_status=Distributor.KycStatus.APPROVED,
        sponsor=root,
    )
    indirect = _make_distributor(
        full_name="Indirect Recruit", ir_id="IR00203", sponsor=direct
    )
    _login(client, root)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Direct Recruit" in content
    assert "IR00202" in content
    assert "Bronze" in content
    assert "Approved" in content
    assert "Indirect Recruit" in content
    assert "IR00203" in content
    ids_in_context = {d.pk for d in response.context["page_obj"].object_list}
    assert ids_in_context == {direct.pk, indirect.pk}


@pytest.mark.django_db
def test_a_distributor_never_sees_another_distributors_team(client):
    """No id/param exists to manipulate -- always scoped to
    request.user.distributor, same IDOR-safe-by-construction pattern as
    earnings_history/binary_tree_view."""
    distributor_a = _make_distributor(full_name="Distributor A", ir_id="IR00204")
    distributor_b = _make_distributor(full_name="Distributor B", ir_id="IR00205")
    _make_distributor(full_name="Child Of B", ir_id="IR00206", sponsor=distributor_b)
    _login(client, distributor_a)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    assert "Child Of B" not in response.content.decode()
    assert response.context["page_obj"].paginator.count == 0


@pytest.mark.django_db
def test_a_sponsor_chain_cycle_does_not_crash_the_page(client):
    """The sponsor self-FK has no DB-level cycle protection. A corrupted
    graph must render a normal page, not hang or 500."""
    root = _make_distributor(full_name="Root", ir_id="IR00207")
    a = _make_distributor(full_name="A", ir_id="IR00208", sponsor=root)
    b = _make_distributor(full_name="B", ir_id="IR00209", sponsor=a)
    Distributor.objects.filter(pk=root.pk).update(sponsor=b)  # cycle
    _login(client, root)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    ids_in_context = {d.pk for d in response.context["page_obj"].object_list}
    assert ids_in_context == {a.pk, b.pk}


@pytest.mark.django_db
def test_the_walk_ceiling_bounds_a_pathologically_deep_chain(client):
    """Patches MAX_MATCHING_BONUS_WALK_DEPTH down to 2 rather than
    constructing 500 real distributors, matching
    tests/unit/commissions/test_walk_sponsor_chain_downline_ids.py's own
    precedent for this exact test."""
    import apps.commissions.services as services_module

    root = _make_distributor(full_name="Root", ir_id="IR00210")
    chain = root
    names_by_level = []
    for i in range(4):
        chain = _make_distributor(full_name=f"Level {i + 1}", sponsor=chain)
        names_by_level.append(chain.full_name)
    _login(client, root)

    with patch.object(services_module, "MAX_MATCHING_BONUS_WALK_DEPTH", 2):
        response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    content = response.content.decode()
    assert names_by_level[0] in content
    assert names_by_level[1] in content
    assert names_by_level[3] not in content  # past the patched ceiling


@pytest.mark.django_db
def test_team_roster_is_paginated_at_twenty_per_page(client):
    root = _make_distributor(full_name="Root", ir_id="IR00211")
    for i in range(25):
        _make_distributor(full_name=f"Recruit {i}", sponsor=root)
    _login(client, root)

    response = client.get(reverse("distributors:team"))

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 25
    assert len(response.context["page_obj"].object_list) == 20
    assert response.context["page_obj"].paginator.num_pages == 2
