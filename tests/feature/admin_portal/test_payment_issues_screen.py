"""Task 67b. The admin-portal screen for payments that need attention.

Task 67 records a PaymentIssue whenever Paystack confirms a payment that did
not become an account, a starter pack or an order, and emails the admin. The
list itself lived only in raw Django Admin -- this is the designed screen,
matching Tasks 26/27/28's own "every admin-facing screen should look like the
rest of the app" precedent.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.distributors.models import PaymentIssue

User = get_user_model()


def _list_url():
    return reverse("admin_portal:payment_issue_list")


def _resolve_url(issue):
    return reverse("admin_portal:payment_issue_resolve", args=[issue.pk])


def _reopen_url(issue):
    return reverse("admin_portal:payment_issue_reopen", args=[issue.pk])


def _make_issue(reference="reg-abc", **fields):
    fields.setdefault("kind", PaymentIssue.Kind.REGISTRATION_UNMATCHED)
    fields.setdefault("amount_pesewas", 10000)
    fields.setdefault("payer_name", "Ama Owusu")
    fields.setdefault("payer_phone", "+233241234567")
    return PaymentIssue.objects.create(reference=reference, **fields)


# --- Permissions ---------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden(client):
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    assert client.get(_list_url()).status_code == 403


@pytest.mark.django_db
def test_a_logged_out_visitor_is_sent_to_the_admin_login(client):
    response = client.get(_list_url())

    assert response.status_code == 302
    assert "login" in response.url


# --- The list ------------------------------------------------------------------


@pytest.mark.django_db
def test_it_shows_the_payer_the_amount_and_what_went_wrong(staff_client):
    _make_issue(detail="No pending registration was found for this payment.")

    response = staff_client.get(_list_url())
    body = response.content.decode()

    assert response.status_code == 200
    assert "reg-abc" in body
    assert "Ama Owusu" in body
    assert "+233241234567" in body
    assert "100.00" in body
    assert "Registration fee paid, no registration found" in body


@pytest.mark.django_db
def test_unresolved_payments_come_first(staff_client):
    resolved = _make_issue("reg-old", resolved_at=timezone.now())
    unresolved = _make_issue("reg-new")
    PaymentIssue.objects.filter(pk=unresolved.pk).update(
        created_at=timezone.now() - timedelta(days=5)
    )

    issues = list(staff_client.get(_list_url()).context["page_obj"].object_list)

    assert [issue.pk for issue in issues] == [unresolved.pk, resolved.pk]


@pytest.mark.django_db
def test_the_empty_state_says_there_is_nothing_to_do(staff_client):
    body = staff_client.get(_list_url()).content.decode()

    assert "No payments need attention" in body


@pytest.mark.django_db
def test_searching_matches_the_reference_name_or_phone(staff_client):
    _make_issue("reg-abc", payer_name="Ama Owusu", payer_phone="+233241234567")
    _make_issue("order-xyz", payer_name="Kofi Mensah", payer_phone="+233209999999")

    for term, expected in [
        ("order-xyz", "order-xyz"),
        ("Kofi", "order-xyz"),
        ("233209", "order-xyz"),
        ("Ama", "reg-abc"),
    ]:
        issues = list(
            staff_client.get(_list_url(), {"q": term}).context["page_obj"].object_list
        )
        assert [issue.reference for issue in issues] == [expected], term


@pytest.mark.django_db
def test_it_can_be_filtered_to_unresolved_or_resolved(staff_client):
    _make_issue("reg-open")
    _make_issue("reg-done", resolved_at=timezone.now())

    for status, expected in [("unresolved", "reg-open"), ("resolved", "reg-done")]:
        issues = list(
            staff_client.get(_list_url(), {"status": status})
            .context["page_obj"]
            .object_list
        )
        assert [issue.reference for issue in issues] == [expected], status


@pytest.mark.django_db
def test_an_unknown_filter_value_does_not_crash(staff_client):
    _make_issue()

    response = staff_client.get(_list_url(), {"status": "../../etc", "page": "abc"})

    assert response.status_code == 200


@pytest.mark.django_db
def test_the_search_returns_just_the_rows_for_htmx(staff_client):
    _make_issue()

    response = staff_client.get(_list_url(), {"q": "reg"}, HTTP_HX_REQUEST="true")
    body = response.content.decode()

    assert "reg-abc" in body
    assert "<html" not in body


@pytest.mark.django_db
def test_the_screen_counts_the_unresolved_payments(staff_client):
    _make_issue("reg-open")
    _make_issue("reg-done", resolved_at=timezone.now())

    assert staff_client.get(_list_url()).context["unresolved_count"] == 1


# --- Marking one sorted ---------------------------------------------------------


@pytest.mark.django_db
def test_an_admin_can_mark_a_payment_sorted(staff_client):
    issue = _make_issue()

    response = staff_client.post(_resolve_url(issue))

    assert response.status_code == 302
    issue.refresh_from_db()
    assert issue.resolved_at is not None


@pytest.mark.django_db
def test_marking_one_sorted_twice_keeps_the_first_time(staff_client):
    issue = _make_issue(resolved_at=timezone.now() - timedelta(days=1))
    first = issue.resolved_at

    staff_client.post(_resolve_url(issue))

    issue.refresh_from_db()
    assert issue.resolved_at == first


@pytest.mark.django_db
def test_an_admin_can_reopen_one_marked_by_mistake(staff_client):
    issue = _make_issue(resolved_at=timezone.now())

    staff_client.post(_reopen_url(issue))

    issue.refresh_from_db()
    assert issue.resolved_at is None


@pytest.mark.django_db
def test_marking_sorted_needs_a_post_not_a_link(staff_client):
    issue = _make_issue()

    assert staff_client.get(_resolve_url(issue)).status_code == 405
    issue.refresh_from_db()
    assert issue.resolved_at is None


@pytest.mark.django_db
def test_a_non_staff_user_cannot_mark_one_sorted(client):
    issue = _make_issue()
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    assert client.post(_resolve_url(issue)).status_code == 403
    issue.refresh_from_db()
    assert issue.resolved_at is None


@pytest.mark.django_db
def test_the_sidebar_links_to_the_screen(staff_client):
    body = staff_client.get(reverse("admin_portal:dashboard")).content.decode()

    assert _list_url() in body
