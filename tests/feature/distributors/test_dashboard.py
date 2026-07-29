from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

import pytest
from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket, PvLedger
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(**overrides):
    """Mirrors test_earnings_history.py's own helper -- every real
    distributor is in the "distributor" group (apps.accounts.permissions.
    is_distributor checks it), so any test helper skipping that would
    exercise a user shape that can never occur in production."""
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_dashboard_requires_login(client):
    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    """Same three-account-types gap test_earnings_history.py already
    guards -- @login_required alone doesn't distinguish a customer
    account (no Distributor row) from a distributor, so
    request.user.distributor would raise an unhandled 500 without an
    explicit is_distributor() guard."""
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_all_stats_render_correctly_for_a_seeded_distributor(client):
    # Code review finding: Distributor.rank is set verbatim from
    # STARTER_PACK_A_RANK/STARTER_PACK_B_RANK (apps/platform_settings/
    # config.py), whose real seeded values are lowercase "bronze"/
    # "silver" -- a capitalized fixture here would mask a template bug
    # that compares against the wrong casing.
    distributor = _make_distributor(
        ir_id="IR-00001", rank="silver", full_name="Ama Mensah"
    )

    # Earnings: only the three bonus types count (Section 6.1: "from all
    # bonus types"), mirroring admin_portal's own existing aggregate.
    credit(
        distributor,
        Decimal("450.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    credit(
        distributor,
        Decimal("120.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="ref-2",
    )
    # A non-earnings credit (a cooling-off refund isn't a "bonus") must
    # not inflate the earnings figures.
    credit(
        distributor,
        Decimal("999.00"),
        transaction_type=WalletTransaction.TransactionType.COOLING_OFF_REFUND,
        reference="ref-3",
    )

    PvLedger.objects.create(distributor=distributor, left_leg_pv=340, right_leg_pv=210)
    MonthlyPersonalPv.objects.create(
        distributor=distributor, period=timezone.now().date().replace(day=1), pv=150
    )

    downline_a = _make_distributor()
    downline_b = _make_distributor()
    BinaryTreeEdge.objects.create(
        ancestor=distributor,
        descendant=downline_a,
        depth=1,
        leg=BinaryTreeEdge.Leg.LEFT,
    )
    BinaryTreeEdge.objects.create(
        ancestor=distributor,
        descendant=downline_b,
        depth=1,
        leg=BinaryTreeEdge.Leg.RIGHT,
    )

    _login(client, distributor)
    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "570.00" in body  # 450 + 120 total earnings, cooling-off excluded
    assert "340" in body  # left leg PV
    assert "210" in body  # right leg PV
    assert "150" in body  # monthly personal PV
    # CodeRabbit: "2" in body is trivially satisfied by unrelated markup
    # (e.g. sm:grid-cols-2, sm:col-span-2), so it wouldn't catch a broken
    # team_size -- assert against context instead, matching this file's
    # own test_team_size_counts_the_entire_downline_both_legs_combined.
    assert response.context["team_size"] == 2
    assert "IR-00001" in body
    assert "Silver" in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "real_rank,expected_display",
    [("bronze", "Bronze"), ("silver", "Silver")],
)
def test_rank_badge_matches_the_real_lowercase_stored_value(
    client, real_rank, expected_display
):
    """Regression guard: the badge previously compared against
    capitalized "Silver"/"Bronze" while every real Distributor.rank
    value (set from STARTER_PACK_A_RANK/STARTER_PACK_B_RANK) is
    lowercase -- every real distributor would silently show as
    "Unranked". Proves both real values render the correct title-cased
    badge text, and neither falls through to "Unranked"."""
    distributor = _make_distributor(rank=real_rank)
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    body = response.content.decode()
    assert expected_display in body
    assert "UNRANKED" not in body.upper()


@pytest.mark.django_db
def test_this_weeks_earnings_excludes_earnings_from_before_the_current_week(client):
    """Section 6.1: "This Week's Earnings" is the CURRENT calendar week --
    a bonus credited last week must not count, even though it counts
    toward the lifetime "Total Earnings" figure."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("200.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="this-week",
    )
    credit(
        distributor,
        Decimal("800.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="last-week",
    )
    WalletTransaction.objects.filter(
        wallet__distributor=distributor, reference="last-week"
    ).update(created_at=timezone.now() - timedelta(days=10))

    _login(client, distributor)
    response = client.get(reverse("distributors:dashboard"))

    context = response.context
    assert context["this_week_earnings"] == Decimal("200.00")
    assert context["total_earnings"] == Decimal("1000.00")


@pytest.mark.django_db
def test_this_weeks_earnings_boundary_is_monday_00_00(client):
    """Code review finding: the prior test only proved a transaction 10
    days old is excluded, never the actual Monday-00:00 boundary itself.
    One transaction one second before it, one one second after -- only
    the latter may count."""
    distributor = _make_distributor()
    now = timezone.now()
    monday_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    credit(
        distributor,
        Decimal("300.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="just-before",
    )
    WalletTransaction.objects.filter(
        wallet__distributor=distributor, reference="just-before"
    ).update(created_at=monday_start - timedelta(seconds=1))

    credit(
        distributor,
        Decimal("500.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="just-after",
    )
    WalletTransaction.objects.filter(
        wallet__distributor=distributor, reference="just-after"
    ).update(created_at=monday_start + timedelta(seconds=1))

    _login(client, distributor)
    response = client.get(reverse("distributors:dashboard"))

    assert response.context["this_week_earnings"] == Decimal("500.00")
    assert response.context["total_earnings"] == Decimal("800.00")


@pytest.mark.django_db
def test_a_brand_new_distributor_with_no_wallet_or_pv_rows_sees_zeros_not_a_500(client):
    """A distributor with zero purchases in their downline has no Wallet
    or PvLedger row yet (both are created lazily on first credit) -- the
    dashboard must render 0s, not crash on a naive .wallet/.pv_ledger
    attribute access (apps/binary_tree/services.py::get_ancestor_pv_
    aggregates already documents exactly this failure mode)."""
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 200
    context = response.context
    assert context["wallet_balance"] == Decimal("0")
    assert context["left_leg_pv"] == 0
    assert context["right_leg_pv"] == 0
    assert context["monthly_personal_pv"] == 0
    assert context["team_size"] == 0
    assert context["total_earnings"] == Decimal("0")
    assert context["this_week_earnings"] == Decimal("0")


@pytest.mark.django_db
def test_team_size_counts_the_entire_downline_both_legs_combined(client):
    """Section 6.1: "Total distributors in their entire downline (left +
    right legs combined)" -- must count descendants at every depth, not
    just direct (depth=1) recruits."""
    distributor = _make_distributor()
    direct_left = _make_distributor()
    direct_right = _make_distributor()
    grandchild = _make_distributor()
    BinaryTreeEdge.objects.create(
        ancestor=distributor,
        descendant=direct_left,
        depth=1,
        leg=BinaryTreeEdge.Leg.LEFT,
    )
    BinaryTreeEdge.objects.create(
        ancestor=distributor,
        descendant=direct_right,
        depth=1,
        leg=BinaryTreeEdge.Leg.RIGHT,
    )
    BinaryTreeEdge.objects.create(
        ancestor=distributor,
        descendant=grandchild,
        depth=2,
        leg=BinaryTreeEdge.Leg.LEFT,
    )

    _login(client, distributor)
    response = client.get(reverse("distributors:dashboard"))

    assert response.context["team_size"] == 3


@pytest.mark.django_db
def test_referral_link_points_to_registration_with_ir_id_prefilled(client):
    """Section 6.1: "Referral Link" is a "personal recruitment link" --
    the registration page's ?ref= param (apps.distributors.views.register)
    is what actually prefills the sponsor field, so the dashboard's link
    must use it."""
    distributor = _make_distributor(ir_id="IR-00042")
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    referral_url = response.context["referral_url"]
    assert referral_url is not None
    assert reverse("distributors:register") in referral_url
    assert "?ref=IR-00042" in referral_url
    assert "https://wa.me/" in response.content.decode()


@pytest.mark.django_db
def test_no_referral_link_for_a_distributor_without_an_ir_id_yet(client):
    """A distributor whose KYC isn't approved yet has no ir_id -- nothing
    to build a real recruitment link from. Must show an honest "not
    available yet" state, not a broken/empty link."""
    distributor = _make_distributor(ir_id=None)
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    assert response.context["referral_url"] is None
    assert "https://wa.me/" not in response.content.decode()


@pytest.mark.django_db
def test_personal_pv_at_or_above_the_threshold_is_marked_eligible(client):
    """Task 20c: the Monthly Personal PV card must reflect the real,
    admin-editable MIN_MONTHLY_PERSONAL_PV threshold (constance), not a
    hardcoded 100 -- and must be dynamic, not always show the mockup's
    happy-path "eligible" state regardless of the real number.

    References config.MIN_MONTHLY_PERSONAL_PV directly rather than a
    hardcoded 100, matching the established convention elsewhere in this
    codebase (tests/unit/pv_ledger/test_personal_pv.py,
    tests/unit/commissions/test_binary_bonus_task.py) -- stays correct
    if the seeded default ever changes."""
    distributor = _make_distributor()
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=timezone.now().date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    assert response.context["is_pv_eligible"] is True
    assert response.context["personal_pv_shortfall"] == 0


@pytest.mark.django_db
def test_personal_pv_below_the_threshold_is_marked_not_yet_eligible(client):
    distributor = _make_distributor()
    shortfall = 65
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=timezone.now().date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV - shortfall,
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    assert response.context["is_pv_eligible"] is False
    assert response.context["personal_pv_shortfall"] == shortfall


@pytest.mark.django_db
def test_days_until_week_reset_is_computed_from_the_real_week_boundary(client):
    """The mockup's "Resetting in 3 days" line must be real, derived from
    the same Monday-00:00 boundary the earnings split already uses, not a
    hardcoded/fabricated number."""
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    now = timezone.now()
    expected = 7 - now.weekday()
    assert response.context["days_until_week_reset"] == expected
    assert 1 <= response.context["days_until_week_reset"] <= 7


@pytest.mark.django_db
def test_carry_forward_pv_shows_the_strong_legs_total_and_expiry(client):
    """Task 21b: the dashboard's Carry-Forward PV card must read real
    PvDailyBucket rows, not a static placeholder, and must show the
    STRONG leg specifically (Section 6.5 of the primary source doc),
    matching apps.pv_ledger.services.get_carry_forward_summary."""
    distributor = _make_distributor()
    today = timezone.now().date()
    PvDailyBucket.objects.create(
        distributor=distributor,
        leg=BinaryTreeEdge.Leg.LEFT,
        date=today - timedelta(days=10),
        pv=900,
    )
    PvDailyBucket.objects.create(
        distributor=distributor,
        leg=BinaryTreeEdge.Leg.RIGHT,
        date=today - timedelta(days=10),
        pv=300,
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    carry_forward = response.context["carry_forward"]
    assert carry_forward.pv == 900
    assert carry_forward.leg == BinaryTreeEdge.Leg.LEFT
    assert carry_forward.nearest_expiry_date is not None
    content = response.content.decode()
    assert "900" in content
    assert "Left" in content


@pytest.mark.django_db
def test_a_distributor_with_no_carried_forward_pv_sees_an_honest_zero_state(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:dashboard"))

    carry_forward = response.context["carry_forward"]
    assert carry_forward.pv == 0
    assert carry_forward.leg is None
    content = response.content.decode()
    assert "No PV carried forward yet" in content
