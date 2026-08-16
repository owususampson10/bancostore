from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model

import pytest
from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.commissions.services import (
    process_binary_bonus_for_distributor,
    process_matching_bonus_for_distributor,
)
from apps.distributors.models import Distributor, IrIdSequence
from apps.distributors.services import (
    _credit_direct_referral_bonus,
    approve_kyc,
    reject_kyc,
)
from apps.notifications.models import Notification, NotificationTemplate
from apps.notifications.sms import fake_outbox
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)


@pytest.fixture(autouse=True)
def _ensure_ir_id_sequence_row(db):
    """Mirrors tests/unit/distributors/test_kyc_review.py's own fixture
    exactly -- a transaction=True test elsewhere in the suite can flush
    this data-migration-seeded row away, and flush doesn't re-run data
    migrations."""
    IrIdSequence.objects.get_or_create(pk=1, defaults={"next_number": 1})


def _make_distributor(**overrides):
    phone = f"+233254{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


def _make_eligible(distributor, run_at=RUN_AT):
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=run_at.date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )


def _bucket(distributor, leg, d, pv):
    return PvDailyBucket.objects.create(distributor=distributor, leg=leg, date=d, pv=pv)


def _set_template_body(key, body):
    """get_or_create, not a bare filter().update() -- a transaction=True
    test's flush doesn't re-run data migrations, so a prior
    transaction=True test in the same session can flush a migration-
    seeded NotificationTemplate row away entirely (see this file's own
    _ensure_ir_id_sequence_row fixture for the identical class of issue
    with IrIdSequence)."""
    template, _ = NotificationTemplate.objects.get_or_create(key=key)
    template.body = body
    template.save()


@pytest.mark.django_db(transaction=True)
def test_a_successful_placement_notifies_the_sponsor():
    sponsor = _make_distributor()
    new_distributor = _make_distributor()

    BinaryTree.place_distributor(sponsor, new_distributor)

    assert Notification.objects.filter(
        distributor=sponsor, event_type=Notification.EventType.DOWNLINE_JOINED
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_a_successful_placement_uses_the_live_admin_edited_template_wording():
    _set_template_body(
        NotificationTemplate.Key.DOWNLINE_JOINED, "Custom: {{name}} joined!"
    )
    sponsor = _make_distributor()
    new_distributor = _make_distributor(full_name="Kwame Asante")

    BinaryTree.place_distributor(sponsor, new_distributor)

    notification = Notification.objects.get(
        distributor=sponsor, event_type=Notification.EventType.DOWNLINE_JOINED
    )
    assert notification.message == "Custom: Kwame Asante joined!"


@pytest.mark.django_db(transaction=True)
def test_root_placement_with_no_sponsor_creates_no_notification():
    root = _make_distributor()

    BinaryTree.place_distributor(None, root)

    assert Notification.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_credited_binary_bonus_notifies_the_distributor():
    distributor = _make_distributor()
    _make_eligible(distributor)
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(distributor, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    amount = process_binary_bonus_for_distributor(distributor, RUN_AT)

    assert amount == Decimal("45.00")
    assert Notification.objects.filter(
        distributor=distributor,
        event_type=Notification.EventType.BINARY_BONUS_CREDITED,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_a_credited_binary_bonus_uses_the_live_admin_edited_template_wording():
    _set_template_body(
        NotificationTemplate.Key.BINARY_BONUS_CREDITED, "Custom: GHS {{amount}}!"
    )
    distributor = _make_distributor()
    _make_eligible(distributor)
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(distributor, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    process_binary_bonus_for_distributor(distributor, RUN_AT)

    notification = Notification.objects.get(
        distributor=distributor,
        event_type=Notification.EventType.BINARY_BONUS_CREDITED,
    )
    assert notification.message == "Custom: GHS 45.00!"


@pytest.mark.django_db(transaction=True)
def test_a_zero_binary_bonus_cycle_creates_no_notification():
    distributor = _make_distributor()
    _make_eligible(distributor)
    # No PV buckets at all -- weak_leg_pv is 0, nothing is owed this cycle.

    amount = process_binary_bonus_for_distributor(distributor, RUN_AT)

    assert amount == Decimal("0.00")
    assert Notification.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_credited_referral_bonus_notifies_the_sponsor():
    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor, starter_pack_pv=1000)

    _credit_direct_referral_bonus(distributor, reference="test-referral-ref")

    assert Notification.objects.filter(
        distributor=sponsor, event_type=Notification.EventType.REFERRAL_BONUS_PAID
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_a_credited_referral_bonus_uses_the_live_admin_edited_inapp_wording():
    _set_template_body(
        NotificationTemplate.Key.DIRECT_REFERRAL_BONUS_CREDITED_INAPP,
        "Custom in-app: GHS {{amount}} from {{referred_name}}!",
    )
    sponsor = _make_distributor()
    distributor = _make_distributor(
        sponsor=sponsor, starter_pack_pv=1000, full_name="Kwame Asante"
    )

    _credit_direct_referral_bonus(distributor, reference="test-referral-ref-2")

    notification = Notification.objects.get(
        distributor=sponsor, event_type=Notification.EventType.REFERRAL_BONUS_PAID
    )
    assert notification.message == "Custom in-app: GHS 100.00 from Kwame Asante!"


@pytest.mark.django_db(transaction=True)
def test_a_credited_referral_bonus_uses_the_live_admin_edited_sms_wording():
    _set_template_body(
        NotificationTemplate.Key.DIRECT_REFERRAL_BONUS_CREDITED_SMS,
        "Custom SMS: GHS {{amount}} from {{referred_name}}!",
    )
    sponsor = _make_distributor()
    distributor = _make_distributor(
        sponsor=sponsor, starter_pack_pv=1000, full_name="Kwame Asante"
    )

    _credit_direct_referral_bonus(distributor, reference="test-referral-ref-3")

    assert fake_outbox[-1]["message"] == "Custom SMS: GHS 100.00 from Kwame Asante!"


@pytest.mark.django_db(transaction=True)
def test_an_approved_withdrawal_notifies_the_distributor():
    from apps.wallet.services import credit
    from apps.withdrawal.services import (
        approve_withdrawal_request,
        submit_withdrawal_request,
    )

    distributor = _make_distributor(
        full_name="Ama Mensah",
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    credit(
        distributor,
        Decimal("1000.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="seed-for-withdrawal-test",
    )
    request = submit_withdrawal_request(distributor, Decimal("500.00"))
    admin = User.objects.create_user(
        username=f"admin-{next(_phone_seq)}", password="Passw0rd!", is_staff=True
    )

    approve_withdrawal_request(request, reviewed_by=admin)

    assert Notification.objects.filter(
        distributor=distributor, event_type=Notification.EventType.WITHDRAWAL_APPROVED
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_an_approved_kyc_notifies_the_distributor():
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.PENDING)

    approve_kyc(distributor)

    assert Notification.objects.filter(
        distributor=distributor, event_type=Notification.EventType.KYC_DECIDED
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_re_approving_an_already_approved_kyc_does_not_double_notify():
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.PENDING)
    approve_kyc(distributor)
    assert Notification.objects.filter(distributor=distributor).count() == 1

    approve_kyc(distributor)  # idempotent no-op -- already approved

    assert Notification.objects.filter(distributor=distributor).count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_rejected_kyc_notifies_the_distributor():
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.PENDING)

    reject_kyc(distributor, reason="Blurry ID photo")

    notification = Notification.objects.get(
        distributor=distributor, event_type=Notification.EventType.KYC_DECIDED
    )
    assert "Blurry ID photo" in notification.message


@pytest.mark.django_db(transaction=True)
def test_an_approved_kyc_uses_the_live_admin_edited_template_wording():
    """Task 48b acceptance criteria: editing a template's wording from
    admin_portal changes the next real send -- not just that the edit
    saves. get_or_create rather than a plain filter().update(), matching
    this file's own _ensure_ir_id_sequence_row fixture's precedent just
    above: a transaction=True test can flush a migration-seeded row
    away entirely (flush doesn't re-run data migrations), so a filter()
    with zero matching rows would silently update nothing."""
    template, _ = NotificationTemplate.objects.get_or_create(
        key=NotificationTemplate.Key.KYC_APPROVED
    )
    template.body = "Custom wording: you're verified!"
    template.save()
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.PENDING)

    approve_kyc(distributor)

    notification = Notification.objects.get(
        distributor=distributor, event_type=Notification.EventType.KYC_DECIDED
    )
    assert notification.message == "Custom wording: you're verified!"


@pytest.mark.django_db(transaction=True)
def test_a_rejected_kyc_uses_the_live_admin_edited_template_wording():
    template, _ = NotificationTemplate.objects.get_or_create(
        key=NotificationTemplate.Key.KYC_REJECTED
    )
    template.body = "Custom wording: not approved -- {{reason}}"
    template.save()
    distributor = _make_distributor(kyc_status=Distributor.KycStatus.PENDING)

    reject_kyc(distributor, reason="Blurry ID photo")

    notification = Notification.objects.get(
        distributor=distributor, event_type=Notification.EventType.KYC_DECIDED
    )
    assert notification.message == "Custom wording: not approved -- Blurry ID photo"


@pytest.mark.django_db(transaction=True)
def test_matching_bonus_never_creates_a_notification():
    """Section 6.6 names the binary bonus and the referral bonus, but not
    the matching bonus -- a deliberate exclusion, not an oversight (see
    tasks/todo.md). Regression guard against future accidental scope
    creep."""
    from datetime import timedelta

    root = _make_distributor(rank="bronze")
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    wallet, _ = Wallet.objects.get_or_create(distributor=level1)
    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount=Decimal("200"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference=f"seed-{level1.pk}",
    )
    WalletTransaction.objects.filter(pk=txn.pk).update(
        created_at=RUN_AT - timedelta(days=1)
    )

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("10.00")
    assert Notification.objects.count() == 0
