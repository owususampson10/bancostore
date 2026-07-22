from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.templatetags.static import static
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit, debit

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    """Mirrors apps/distributors/services.py::consume_paid_registration's
    real account-creation shape -- it adds every real distributor to the
    "distributor" group (apps/accounts/permissions.py::is_distributor
    checks that group), so this test helper must too, or every test here
    would exercise a user shape that can never occur in production."""
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_earnings_history_requires_login(client):
    response = client.get(reverse("distributors:earnings_history"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    """Security review (2026-07-22): this platform has three account types
    sharing one User model (customer/distributor/admin) -- @login_required
    alone doesn't distinguish them. A logged-in User with no Distributor
    row (e.g. a customer account) must get a clean 403, not an unhandled
    500 from request.user.distributor raising DoesNotExist."""
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:earnings_history"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_page_loads_the_shared_js_bundle_so_the_sidebar_can_actually_collapse(client):
    """Regression guard: templates/distributors/base_dashboard.html shipped
    without a <script src=".../main.js"> tag, so Alpine.js (bundled into
    main.js -- see static/src/main.js) never loaded on this page. Every
    x-data/x-on:click/:class binding on the collapsible sidebar was
    therefore inert -- the toggle button rendered but did nothing, caught
    only by an actual browser check (a curl-based check of the rendered
    HTML can't catch this, since the markup itself is valid; only the
    client-side behavior was broken). pytest can't drive a real browser
    either, but it can assert the one thing that made the bug possible:
    the script tag must be present."""
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    assert f'src="{static("assets/main.js")}"' in response.content.decode()


@pytest.mark.django_db
def test_shows_own_wallet_balance_and_summary_totals(client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("450.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    credit(
        distributor,
        Decimal("1200.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="ref-2",
    )
    debit(
        distributor,
        Decimal("500.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-3",
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "GHS 1,150.00" in body  # current balance: 450 + 1200 - 500
    assert "GHS 1,650.00" in body  # total earned: 450 + 1200
    assert "GHS 500.00" in body  # total withdrawn
    assert "Aggregated bonuses &amp; referrals" in body


@pytest.mark.django_db
def test_shows_the_date_of_the_most_recent_withdrawal(client):
    """CodeRabbit review, 2026-07-22: the original version of this test
    re-queried the same WalletTransaction table with the same ordering
    the view itself uses and compared the two -- self-referential, so it
    would pass even if the view picked the wrong row, as long as it picked
    it *consistently* wrong. auto_now_add also means both debits would
    land within the same second in a fast test run, meaning "order by
    -created_at" ties would be broken arbitrarily by the database, not
    deterministically. This version gives each transaction an explicit,
    widely-separated date (including a newer *non*-withdrawal transaction,
    proving the view filters by type and doesn't just grab the single most
    recent transaction of any kind) and asserts the literal expected date
    -- a hardcoded value, not a re-derived one."""
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("1000.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    debit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-2",
    )
    debit(
        distributor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-3",
    )
    credit(
        distributor,
        Decimal("20.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-4",
    )
    # auto_now_add ignores any created_at passed at creation time -- update
    # each row explicitly afterward instead.
    older_txn = WalletTransaction.objects.get(reference="ref-2")
    newer_txn = WalletTransaction.objects.get(reference="ref-3")
    newest_txn = WalletTransaction.objects.get(reference="ref-4")
    WalletTransaction.objects.filter(pk=older_txn.pk).update(
        created_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
    )
    WalletTransaction.objects.filter(pk=newer_txn.pk).update(
        created_at=datetime(2026, 3, 15, tzinfo=dt_timezone.utc)
    )
    WalletTransaction.objects.filter(pk=newest_txn.pk).update(
        created_at=datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    body = response.content.decode()
    assert "Last withdrawal: Mar 15, 2026" in body
    assert "Last withdrawal: Jan 01, 2026" not in body
    assert "Last withdrawal: Jun 01, 2026" not in body


@pytest.mark.django_db
def test_shows_no_withdrawal_yet_when_distributor_has_never_withdrawn(client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("450.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    body = response.content.decode()
    assert "No withdrawals yet" in body
    assert "Last withdrawal:" not in body


@pytest.mark.django_db
def test_lists_transactions_with_type_signed_amount_and_date(client):
    distributor = _make_distributor()
    credit(
        distributor,
        Decimal("450.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="ref-1",
    )
    debit(
        distributor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="ref-2",
    )
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    body = response.content.decode()
    assert "Direct Referral Bonus" in body
    assert "Withdrawal Debit" in body
    assert "+GHS 450.00" in body
    assert "-GHS 100.00" in body


@pytest.mark.django_db
def test_never_shows_another_distributors_transactions(client):
    """A distributor must only ever see their own ledger — there is no
    id/reference taken from the URL or query params, the query is always
    scoped to request.user.distributor, so this is the regression guard
    proving that scoping actually holds."""
    own = _make_distributor()
    other = _make_distributor()
    credit(
        own,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="own-ref",
    )
    credit(
        other,
        Decimal("999.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="other-ref",
    )
    _login(client, own)

    response = client.get(reverse("distributors:earnings_history"))

    body = response.content.decode()
    assert "GHS 999.00" not in body
    assert "other-ref" not in body


@pytest.mark.django_db
def test_shows_empty_state_when_distributor_has_no_transactions(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:earnings_history"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "No earnings yet" in body
    assert "GHS 0.00" in body  # current balance for a never-credited wallet


@pytest.mark.django_db
def test_paginates_transactions(client):
    distributor = _make_distributor()
    for i in range(25):
        credit(
            distributor,
            Decimal("10.00"),
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference=f"ref-{i}",
        )
    _login(client, distributor)

    page_one = client.get(reverse("distributors:earnings_history"))
    page_two = client.get(reverse("distributors:earnings_history"), {"page": 2})

    assert page_one.status_code == 200
    assert page_two.status_code == 200
    assert page_one.context["page_obj"].has_next() is True
    assert page_two.context["page_obj"].number == 2
    assert (
        len(page_one.context["page_obj"].object_list)
        == page_one.context["page_obj"].paginator.per_page
    )
