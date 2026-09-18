from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password

import pytest
from constance import config

from apps.distributors.models import Distributor, PaymentIssue, PendingRegistration
from apps.distributors.payment_outcomes import PaymentOutcome
from apps.distributors.paystack import PaystackError, PaystackNotFoundError
from apps.distributors.services import consume_paid_registration

User = get_user_model()

_phone_seq = count(1)


def _make_sponsor():
    phone = f"+233209{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, ir_id="IR00001")


def _make_pending(sponsor, reference="ref-1", fee_pesewas=10000, phone=None):
    return PendingRegistration.objects.create(
        full_name="Kofi Mensah",
        phone_number=phone or f"+233241{next(_phone_seq):06d}",
        email="kofi@example.test",
        address="12 Ring Road",
        area="Osu",
        landmark="",
        password_hash=make_password("S3cure-Passw0rd!"),
        sponsor=sponsor,
        payment_reference=reference,
        fee_amount_pesewas=fee_pesewas,
    )


def _success_verify(amount=10000, currency="GHS"):
    return {"status": "success", "amount": amount, "currency": currency}


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_successful_verification_creates_account_and_marks_consumed(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is not None
    distributor = Distributor.objects.get(phone_number=pending.phone_number)
    assert distributor.sponsor_id == sponsor.id
    assert distributor.full_name == "Kofi Mensah"
    assert distributor.address == "12 Ring Road"
    assert distributor.user.groups.filter(name="distributor").exists()
    assert check_password("S3cure-Passw0rd!", distributor.user.password)


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_already_consumed_is_an_idempotent_no_op(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")
    consume_paid_registration("ref-1")  # simulates a duplicate webhook delivery

    assert Distributor.objects.filter(phone_number=pending.phone_number).count() == 1
    # The consumed_at check short-circuits before re-verifying with
    # Paystack, so a duplicate webhook delivery costs no extra API call.
    assert mock_verify.call_count == 1


@pytest.mark.django_db
def test_missing_pending_registration_does_not_crash():
    consume_paid_registration("no-such-reference")

    assert not Distributor.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_non_success_status_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = {"status": "failed", "amount": 10000, "currency": "GHS"}

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is None
    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_wrong_currency_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify(currency="NGN")

    consume_paid_registration("ref-1")

    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_is_checked_against_the_snapshotted_fee_not_live_constance(mock_verify):
    """Regression test for a real bug found via doubt-driven-development: if
    an admin changes REGISTRATION_FEE between initialize and webhook, a
    genuinely, correctly paid registration must not fail because the
    amount is compared against a freshly re-read constance value instead
    of the fee that was actually in effect (and paid) at initialize time."""
    sponsor = _make_sponsor()
    # Snapshotted at (simulated) initialize time, when the fee was 100 GHS.
    pending = _make_pending(sponsor, fee_pesewas=10000)
    # The customer genuinely paid the amount in effect when they checked
    # out (10000 pesewas). Verify confirms that's what Paystack actually
    # processed.
    mock_verify.return_value = _success_verify(amount=10000)

    # An admin changes the live fee *after* checkout but before the webhook
    # arrives. If the amount check re-read this live value instead of the
    # snapshot, this genuinely correct payment would wrongly fail.
    config.REGISTRATION_FEE = Decimal("250")

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is not None
    assert Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_mismatch_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor, fee_pesewas=10000)
    mock_verify.return_value = _success_verify(amount=5000)

    consume_paid_registration("ref-1")

    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_existing_distributor_with_same_phone_prevents_duplicate_creation(mock_verify):
    sponsor = _make_sponsor()
    phone = f"+233241{next(_phone_seq):06d}"
    existing_user = User.objects.create_user(username=phone, password="x")
    Distributor.objects.create(user=existing_user, phone_number=phone)
    pending = _make_pending(sponsor, phone=phone)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is None
    assert Distributor.objects.filter(phone_number=phone).count() == 1


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paystack_verify_error_does_not_crash(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.side_effect = PaystackError("timed out")

    consume_paid_registration("ref-1")  # must not raise

    pending.refresh_from_db()
    assert pending.consumed_at is None


# --- Task 67: a successful payment always ends in an account or an issue -----
#
# Found 2026-09-16: a registration fee paid on a checkout page left open past
# cleanup created nothing, and the only trace was a log line.

Outcome = PaymentOutcome


@pytest.fixture(autouse=True)
def _run_on_commit_now(monkeypatch):
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


def _reg_reference(pending, suffix="aaaaaaaa"):
    return f"reg-{pending.token.hex}-{suffix}"


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_creating_the_account_records_which_reference_paid(mock_verify):
    pending = _make_pending(_make_sponsor())
    mock_verify.return_value = _success_verify()

    assert consume_paid_registration("ref-1") == Outcome.APPLIED

    pending.refresh_from_db()
    assert pending.consumed_reference == "ref-1"


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_payment_with_no_registration_at_all_records_an_issue(mock_verify):
    """The 2026-09-16 incident: the registration row had been deleted."""
    mock_verify.return_value = _success_verify() | {
        "metadata": {"full_name": "Ama Owusu", "phone_number": "+233241234567"}
    }
    reference = "reg-a7dc13a96d844de081e5ba21dc5cadfc-80151a91"

    assert consume_paid_registration(reference) == Outcome.ISSUE_RECORDED

    issue = PaymentIssue.objects.get(reference=reference)
    assert issue.kind == PaymentIssue.Kind.REGISTRATION_UNMATCHED
    assert issue.payer_name == "Ama Owusu"


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_unpaid_reference_with_no_registration_records_nothing(mock_verify):
    mock_verify.return_value = {"status": "abandoned"}

    outcome = consume_paid_registration("reg-a7dc13a96d844de081e5ba21dc5cadfc-80151a91")

    assert outcome == Outcome.NOT_PAID
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_malformed_reference_is_never_sent_to_paystack(mock_verify):
    """A public callback URL takes any reference -- only our own shape is
    worth an authenticated API call."""
    assert consume_paid_registration("reg-../../x") == Outcome.UNKNOWN_REFERENCE
    mock_verify.assert_not_called()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_payment_on_an_older_checkout_tab_still_creates_the_account(mock_verify):
    """Every visit to the payment step issues a new reference; a person who
    pays on the tab they opened first must not lose their money."""
    pending = _make_pending(_make_sponsor(), reference=None)
    pending.payment_reference = _reg_reference(pending, "bbbbbbbb")
    pending.save()
    older_reference = _reg_reference(pending, "aaaaaaaa")
    mock_verify.return_value = _success_verify()

    assert consume_paid_registration(older_reference) == Outcome.APPLIED

    pending.refresh_from_db()
    assert pending.consumed_reference == older_reference
    assert Distributor.objects.filter(phone_number=pending.phone_number).exists()
    mock_verify.assert_called_once_with(older_reference)


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_replays_of_the_reference_that_paid_are_silent(mock_verify):
    pending = _make_pending(_make_sponsor(), reference=None)
    pending.payment_reference = _reg_reference(pending, "bbbbbbbb")
    pending.save()
    older_reference = _reg_reference(pending, "aaaaaaaa")
    mock_verify.return_value = _success_verify()
    consume_paid_registration(older_reference)
    mock_verify.reset_mock()

    assert consume_paid_registration(older_reference) == Outcome.ALREADY_APPLIED

    mock_verify.assert_not_called()
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paying_twice_records_a_duplicate_issue(mock_verify):
    pending = _make_pending(_make_sponsor(), reference=None)
    first = _reg_reference(pending, "aaaaaaaa")
    second = _reg_reference(pending, "bbbbbbbb")
    pending.payment_reference = second
    pending.save()
    mock_verify.return_value = _success_verify()
    consume_paid_registration(first)

    assert consume_paid_registration(second) == Outcome.ISSUE_RECORDED

    issue = PaymentIssue.objects.get(reference=second)
    assert issue.kind == PaymentIssue.Kind.REGISTRATION_DUPLICATE
    assert Distributor.objects.filter(phone_number=pending.phone_number).count() == 1


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_unpaid_second_reference_is_not_a_duplicate(mock_verify):
    pending = _make_pending(_make_sponsor())
    mock_verify.return_value = _success_verify()
    consume_paid_registration("ref-1")
    mock_verify.return_value = {"status": "abandoned"}

    outcome = consume_paid_registration(_reg_reference(pending, "cccccccc"))

    assert outcome == Outcome.NOT_PAID
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_mismatch_on_a_paid_reference_records_an_issue(mock_verify):
    pending = _make_pending(_make_sponsor(), fee_pesewas=10000)
    mock_verify.return_value = _success_verify(amount=5000)

    assert consume_paid_registration("ref-1") == Outcome.ISSUE_RECORDED

    issue = PaymentIssue.objects.get(reference="ref-1")
    assert issue.kind == PaymentIssue.Kind.REGISTRATION_NOT_CREATED
    assert issue.payer_phone == str(pending.phone_number)


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_existing_distributor_phone_records_an_issue(mock_verify):
    sponsor = _make_sponsor()
    _make_pending(sponsor, phone=str(sponsor.phone_number))
    mock_verify.return_value = _success_verify()

    assert consume_paid_registration("ref-1") == Outcome.ISSUE_RECORDED

    assert (
        PaymentIssue.objects.get(reference="ref-1").kind
        == PaymentIssue.Kind.REGISTRATION_NOT_CREATED
    )


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_an_account_creation_error_records_an_issue_and_rolls_back(mock_verify):
    """The IntegrityError has to be caught inside its own savepoint -- without
    one, the outer transaction is marked for rollback and the issue (written
    after) would be lost with it."""
    pending = _make_pending(_make_sponsor())
    User.objects.create_user(username=str(pending.phone_number), password="x")
    mock_verify.return_value = _success_verify()

    assert consume_paid_registration("ref-1") == Outcome.ISSUE_RECORDED

    pending.refresh_from_db()
    assert pending.consumed_at is None
    assert (
        PaymentIssue.objects.get(reference="ref-1").kind
        == PaymentIssue.Kind.REGISTRATION_NOT_CREATED
    )


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_verify_failure_is_reported_so_the_webhook_can_ask_for_a_retry(
    mock_verify,
):
    _make_pending(_make_sponsor())
    mock_verify.side_effect = PaystackError("timed out")

    assert consume_paid_registration("ref-1") == Outcome.VERIFY_FAILED
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_a_reference_paystack_never_saw_is_not_paid(mock_verify):
    _make_pending(_make_sponsor())
    mock_verify.side_effect = PaystackNotFoundError("not found")

    assert consume_paid_registration("ref-1") == Outcome.NOT_PAID


@pytest.mark.django_db
@patch("apps.distributors.services.record_payment_issue", return_value=None)
@patch("apps.distributors.services.verify_transaction")
def test_an_issue_that_could_not_be_saved_is_not_reported_as_handled(
    mock_verify, mock_record
):
    """Code review: otherwise the webhook acknowledges it and cleanup deletes
    the only copy of the payer's details."""
    mock_verify.return_value = _success_verify()

    outcome = consume_paid_registration("reg-a7dc13a96d844de081e5ba21dc5cadfc-80151a91")

    assert outcome == Outcome.VERIFY_FAILED
    mock_record.assert_called_once()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_the_fee_is_checked_against_the_checkout_that_was_paid(mock_verify):
    """Adversarial security review: the fee snapshot is overwritten on every
    visit to the payment step, so an admin changing REGISTRATION_FEE between
    a tab opening and its payment would reject a perfectly good payment --
    money captured, no account."""
    from apps.distributors.models import RegistrationCheckout

    pending = _make_pending(_make_sponsor(), reference=None, fee_pesewas=15000)
    old_reference = f"reg-{pending.token.hex}-aaaaaaaa"
    RegistrationCheckout.objects.create(
        pending_registration=pending,
        reference=old_reference,
        fee_amount_pesewas=10000,  # the fee when that tab was opened
    )
    pending.payment_reference = f"reg-{pending.token.hex}-bbbbbbbb"
    pending.save()
    mock_verify.return_value = _success_verify(amount=10000)

    assert consume_paid_registration(old_reference) == Outcome.APPLIED

    assert Distributor.objects.filter(phone_number=pending.phone_number).exists()
