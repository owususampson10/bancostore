from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.distributors.models import DiditVerification, Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import submit_withdrawal_request
from tests.conftest import WEASYPRINT_AVAILABLE

User = get_user_model()
_phone_seq = count(1)


def _make_eligible_distributor(balance=Decimal("1000.00"), full_name="Ama Mensah"):
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name=full_name,
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    if balance > 0:
        credit(
            distributor,
            balance,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference=f"seed-{distributor.pk}",
        )
    return distributor


def _make_submitted_request(distributor, amount=Decimal("500.00")):
    return submit_withdrawal_request(distributor, amount)


def _queue_url():
    return reverse("admin_portal:withdrawal_review_queue")


def _detail_url(withdrawal_request):
    return reverse(
        "admin_portal:withdrawal_review_detail", args=[withdrawal_request.pk]
    )


@pytest.mark.django_db
def test_queue_falls_back_to_the_extracted_id_name_when_full_name_is_blank(
    staff_client,
):
    """Bug reported live (2026-07-23): a distributor with no full_name set
    (only ever populated from PendingRegistration at registration time)
    showed "(No name on file)" here even though every distributor
    reaching this screen has an approved DiditVerification -- the real
    name is sitting right there in extracted_full_name. Mirrors the
    identical fallback already proven for KYC review."""
    distributor = _make_eligible_distributor(full_name="")
    DiditVerification.objects.create(
        distributor=distributor,
        session_id=f"sess-{distributor.pk}",
        extracted_full_name="Kojo Antwi",
    )
    _make_submitted_request(distributor)

    response = staff_client.get(_queue_url())

    assert b"Kojo Antwi" in response.content
    assert b"(No name on file)" not in response.content


@pytest.mark.django_db
def test_detail_falls_back_to_the_extracted_id_name_when_full_name_is_blank(
    staff_client,
):
    distributor = _make_eligible_distributor(full_name="")
    DiditVerification.objects.create(
        distributor=distributor,
        session_id=f"sess-{distributor.pk}",
        extracted_full_name="Kojo Antwi",
    )
    request = _make_submitted_request(distributor)

    response = staff_client.get(_detail_url(request))

    assert b"Kojo Antwi" in response.content
    assert b"(No name on file)" not in response.content


@pytest.mark.django_db
def test_queue_lists_a_submitted_withdrawal_request(staff_client):
    distributor = _make_eligible_distributor(full_name="Kofi Owusu")
    _make_submitted_request(distributor, Decimal("500.00"))

    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    body = response.content.decode()
    assert "Kofi Owusu" in body
    assert "500.00" in body
    assert "495.00" in body  # net after 1% withholding tax


@pytest.mark.django_db
def test_queue_summary_cards_show_real_totals_not_the_stitch_mockup_numbers(
    staff_client,
):
    """The Stitch design's stat cards had fabricated numbers (e.g. "42
    pending", "GHS 124.5k") -- these must reflect the actual queryset, not
    a static/hardcoded placeholder."""
    first = _make_eligible_distributor(full_name="First Distributor")
    second = _make_eligible_distributor(full_name="Second Distributor")
    _make_submitted_request(first, Decimal("500.00"))  # net 495.00
    _make_submitted_request(second, Decimal("300.00"))  # net 297.00

    response = staff_client.get(_queue_url())

    body = response.content.decode()
    assert response.status_code == 200
    assert ">2<" in body  # pending_count
    assert "792.00" in body  # 495.00 + 297.00


@pytest.mark.django_db
def test_queue_summary_cards_do_not_render_when_nothing_is_pending(staff_client):
    """Cards render alongside the table, not standalone -- an empty queue
    shows the empty state instead of "0 pending / GHS 0.00 / 0 high
    priority" cards with nothing underneath them."""
    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    assert b"Pending Requests" not in response.content
    assert b"Total Net Payout" not in response.content
    assert b"High Priority" not in response.content


@pytest.mark.django_db
def test_high_priority_card_counts_requests_above_the_threshold(staff_client):
    """Deliberately does not claim these "require senior admin approval"
    (the original Stitch mockup's copy) -- this codebase has no
    multi-tier admin permission system, any staff admin can approve any
    amount. It's a display-only nudge, not an approval gate."""
    from constance import config

    config.HIGH_PRIORITY_WITHDRAWAL_THRESHOLD = Decimal("5000")
    above = _make_eligible_distributor(balance=Decimal("10000.00"))
    below = _make_eligible_distributor(balance=Decimal("10000.00"))
    _make_submitted_request(above, Decimal("6000.00"))
    _make_submitted_request(below, Decimal("500.00"))

    response = staff_client.get(_queue_url())

    body = response.content.decode()
    assert response.status_code == 200
    assert "require senior admin approval" not in body
    assert "over GHS 5000" in body
    assert ">1 <span" in body  # exactly one request above the threshold


@pytest.mark.django_db
def test_queue_excludes_an_already_approved_request(staff_client):
    distributor = _make_eligible_distributor(full_name="Already Approved")
    from apps.withdrawal.services import approve_withdrawal_request

    request = _make_submitted_request(distributor)
    admin_user = User.objects.create_user(username="admin-reviewer", password="x")
    approve_withdrawal_request(request, reviewed_by=admin_user)

    response = staff_client.get(_queue_url())

    assert b"Already Approved" not in response.content


@pytest.mark.django_db
def test_queue_shows_an_empty_state_when_nothing_is_pending(staff_client):
    response = staff_client.get(_queue_url())

    assert response.status_code == 200
    assert b"No pending withdrawal requests right now." in response.content


@pytest.mark.django_db
def test_detail_shows_the_request_and_distributor_data(staff_client):
    distributor = _make_eligible_distributor(
        full_name="Kwame Asante", balance=Decimal("2000.00")
    )
    _make_submitted_request(distributor, Decimal("500.00"))
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.get(_detail_url(request))

    body = response.content.decode()
    assert response.status_code == 200
    assert "Kwame Asante" in body
    assert "500.00" in body
    assert "495.00" in body
    assert "5.00" in body  # tax
    assert "+233247111222" in body
    assert "MTN MoMo" in body
    assert "2000.00" in body  # wallet balance


@pytest.mark.django_db
def test_approving_from_the_detail_screen_debits_the_wallet(staff_client):
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    _make_submitted_request(distributor, Decimal("500.00"))
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.post(
        _detail_url(request), {"action": "approve"}, follow=True
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.APPROVED_DEBITED
    distributor.wallet.refresh_from_db()
    assert distributor.wallet.balance == Decimal("505.00")


@pytest.mark.django_db
def test_approve_flash_message_shows_the_real_name_not_the_debug_repr(staff_client):
    """code-review finding: f"...for {distributor}." was interpolating
    Distributor.__str__ ("Distributor<+233...>") instead of the proper
    fallback display name computed elsewhere in the same view."""
    distributor = _make_eligible_distributor(
        full_name="Efua Asante", balance=Decimal("1000.00")
    )
    _make_submitted_request(distributor, Decimal("500.00"))
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.post(
        _detail_url(request), {"action": "approve"}, follow=True
    )

    body = response.content.decode()
    assert "Approved withdrawal request for Efua Asante." in body
    assert "Distributor<" not in body


@pytest.mark.django_db
def test_detail_uses_the_configured_withdrawal_day_not_a_hardcoded_friday(
    staff_client,
):
    """code-review finding: the approve modal's copy hardcoded "Friday"
    even though WITHDRAWAL_DAY is a live, admin-editable setting."""
    from constance import config

    config.WITHDRAWAL_DAY = "wednesday"
    distributor = _make_eligible_distributor()
    _make_submitted_request(distributor)
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.get(_detail_url(request))

    body = response.content.decode()
    assert "Wednesday batch" in body
    assert "Friday batch" not in body


@pytest.mark.django_db
def test_rejecting_from_the_detail_screen_requires_a_reason(staff_client):
    distributor = _make_eligible_distributor()
    _make_submitted_request(distributor)
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.post(_detail_url(request), {"action": "reject"})

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.SUBMITTED


@pytest.mark.django_db
def test_rejecting_from_the_detail_screen_with_a_reason_rejects_and_never_debits(
    staff_client,
):
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    _make_submitted_request(distributor, Decimal("500.00"))
    request = WithdrawalRequest.objects.get(distributor=distributor)

    response = staff_client.post(
        _detail_url(request),
        {"action": "reject", "reason": "Payout details could not be verified."},
        follow=True,
    )

    assert response.status_code == 200
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.REJECTED
    assert request.rejection_reason == "Payout details could not be verified."
    distributor.wallet.refresh_from_db()
    assert distributor.wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_detail_404s_for_a_request_that_is_no_longer_submitted(staff_client):
    """Mirrors the KYC review detail screen's own direct-URL-bypass fix --
    a request that's already been decided shouldn't be reachable for a
    fresh approve/reject POST."""
    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    from apps.withdrawal.services import approve_withdrawal_request

    request = _make_submitted_request(distributor, Decimal("500.00"))
    admin_user = User.objects.create_user(username="admin-reviewer-2", password="x")
    approve_withdrawal_request(request, reviewed_by=admin_user)

    response = staff_client.get(_detail_url(request))

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Task 45: CSV export -- the withdrawal review queue as bancostore.exports'
# first real consumer.
# ---------------------------------------------------------------------------


def _export_url():
    return reverse("admin_portal:withdrawal_review_export")


@pytest.mark.django_db
def test_export_csv_contains_the_pending_withdrawal_requests(staff_client):
    distributor = _make_eligible_distributor(full_name="Ama Mensah")
    distributor.ir_id = "IR00042"
    distributor.save(update_fields=["ir_id"])
    _make_submitted_request(distributor, amount=Decimal("500.00"))

    response = staff_client.get(_export_url())

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    body = response.content.decode()
    assert "Ama Mensah" in body
    assert "IR00042" in body
    assert "500.00" in body


@pytest.mark.django_db
def test_export_csv_only_includes_submitted_requests(staff_client):
    """Matches the queue's own scope exactly (Task 23's own docstring:
    "Only status=SUBMITTED requests belong in a review queue") -- an
    export of this screen must never silently include an
    already-decided request the admin has nothing left to act on."""
    from apps.withdrawal.services import approve_withdrawal_request

    pending_distributor = _make_eligible_distributor(full_name="Ama Mensah")
    _make_submitted_request(pending_distributor, amount=Decimal("500.00"))
    decided_distributor = _make_eligible_distributor(full_name="Kojo Antwi")
    decided_request = _make_submitted_request(
        decided_distributor, amount=Decimal("300.00")
    )
    admin_user = User.objects.create_user(username="admin-export-1", password="x")
    approve_withdrawal_request(decided_request, reviewed_by=admin_user)

    response = staff_client.get(_export_url())

    body = response.content.decode()
    assert "Ama Mensah" in body
    assert "Kojo Antwi" not in body


@pytest.mark.django_db
def test_export_csv_neutralizes_formula_injection_in_distributor_name(staff_client):
    """OWASP CSV injection, same risk class as distributor_directory_export
    (Task 23 follow-up) -- full_name is free text a distributor sets
    themselves at registration."""
    distributor = _make_eligible_distributor(
        full_name='=HYPERLINK("https://evil.example")'
    )
    _make_submitted_request(distributor, amount=Decimal("500.00"))

    response = staff_client.get(_export_url())

    body = response.content.decode()
    assert "'=HYPERLINK" in body


@pytest.mark.django_db
def test_export_csv_requires_staff(client, db):
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_export_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login_for_export(client, db):
    response = client.get(_export_url())

    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Checkpoint O finding: SPEC_PHASE2.md Feature 7's success criteria require
# the GRA withholding-tax export be downloadable as both CSV and PDF, same
# as every Feature 5 report -- this screen shipped CSV-only in Task 45.
# Mirrors sales_revenue_report_export_pdf's exact same
# export_as_pdf/WeasyPrint/pytest.mark.skipif(not WEASYPRINT_AVAILABLE)
# shape (tests/feature/reporting/test_sales_revenue_report.py).
# ---------------------------------------------------------------------------


def _pdf_export_url():
    return reverse("admin_portal:withdrawal_review_export_pdf")


@pytest.mark.django_db
@pytest.mark.skipif(
    not WEASYPRINT_AVAILABLE,
    reason="WeasyPrint needs the system Pango library, not installable locally.",
)
def test_export_pdf_generates_a_real_pdf(staff_client):
    distributor = _make_eligible_distributor(full_name="Ama Mensah")
    _make_submitted_request(distributor, amount=Decimal("500.00"))

    response = staff_client.get(_pdf_export_url())

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


@pytest.mark.django_db
def test_export_pdf_requires_staff(client, db):
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_pdf_export_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login_for_pdf_export(client, db):
    response = client.get(_pdf_export_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url
    assert reverse("two_factor:login") in response.url


@pytest.mark.django_db
def test_a_non_staff_authenticated_user_is_forbidden(client, db):
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(_queue_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_user_is_redirected_to_login(client, db):
    response = client.get(_queue_url())

    assert response.status_code == 302
    assert reverse("two_factor:login") in response.url
