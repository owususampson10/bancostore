from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest

from apps.commissions.services import walk_sponsor_chain_downline_ids
from apps.distributors.models import Distributor

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(sponsor=None):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


@pytest.mark.django_db
def test_a_distributor_with_no_downline_returns_an_empty_set():
    root = _make_distributor()

    result = walk_sponsor_chain_downline_ids(root)

    assert result == set()


@pytest.mark.django_db
def test_max_depth_zero_returns_an_empty_set_without_querying():
    root = _make_distributor()
    _make_distributor(sponsor=root)

    result = walk_sponsor_chain_downline_ids(root, max_depth=0)

    assert result == set()


@pytest.mark.django_db
def test_unlimited_depth_walks_the_full_multi_level_chain():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    level2a = _make_distributor(sponsor=level1)
    level2b = _make_distributor(sponsor=level1)
    level3 = _make_distributor(sponsor=level2a)

    result = walk_sponsor_chain_downline_ids(root, max_depth=None)

    assert result == {level1.pk, level2a.pk, level2b.pk, level3.pk}
    assert root.pk not in result


@pytest.mark.django_db
def test_max_depth_bounds_the_walk_to_that_many_levels():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    level2 = _make_distributor(sponsor=level1)
    _make_distributor(sponsor=level2)  # level 3, out of range

    result = walk_sponsor_chain_downline_ids(root, max_depth=2)

    assert result == {level1.pk, level2.pk}


@pytest.mark.django_db
def test_a_sponsor_chain_cycle_does_not_infinite_loop():
    """Distributor.sponsor is a plain self-FK with no DB-level cycle
    protection (unlike BinaryTreeEdge). A corrupted graph must terminate,
    not hang, and each id should only ever appear once in the result."""
    root = _make_distributor()
    a = _make_distributor(sponsor=root)
    b = _make_distributor(sponsor=a)
    Distributor.objects.filter(pk=root.pk).update(sponsor=b)  # root -> a -> b -> root

    result = walk_sponsor_chain_downline_ids(root, max_depth=None)

    assert result == {a.pk, b.pk}


@pytest.mark.django_db
def test_the_walk_ceiling_bounds_an_unlimited_depth_walk():
    """Patches MAX_MATCHING_BONUS_WALK_DEPTH down to 2 rather than
    constructing 500 real distributors -- the mechanism being tested (the
    ceiling check inside the loop) doesn't care what the actual number is,
    matching test_matching_bonus.py's own precedent for this exact test."""
    import apps.commissions.services as services_module

    root = _make_distributor()
    chain = root
    ids_by_level = []
    for _ in range(4):
        chain = _make_distributor(sponsor=chain)
        ids_by_level.append(chain.pk)

    with patch.object(services_module, "MAX_MATCHING_BONUS_WALK_DEPTH", 2):
        result = walk_sponsor_chain_downline_ids(root, max_depth=None)

    assert result == set(ids_by_level[:2])
