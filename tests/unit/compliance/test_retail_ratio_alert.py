from decimal import Decimal
from itertools import count
from unittest.mock import patch

import pytest
from constance import config

from apps.compliance.models import ComplianceAlertState
from apps.compliance.services import (
    check_retail_ratio_and_alert,
    get_retail_distributor_ratio,
)
from apps.orders.models import Order

_ref_seq = count(1)


def _make_order(*, pv_earned=0, status=Order.Status.CONFIRMED):
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference=f"retail-ratio-test-{next(_ref_seq)}",
        status=status,
        pv_earned=pv_earned,
    )


# ---------------------------------------------------------------------------
# get_retail_distributor_ratio
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_returns_none_with_no_paid_orders():
    assert get_retail_distributor_ratio() is None


@pytest.mark.django_db
def test_computes_the_retail_percentage():
    _make_order(pv_earned=0)
    _make_order(pv_earned=0)
    _make_order(pv_earned=0)
    _make_order(pv_earned=60)

    assert get_retail_distributor_ratio() == Decimal("75.00")


@pytest.mark.django_db
def test_excludes_pending_cancelled_and_refunded_orders():
    _make_order(pv_earned=0, status=Order.Status.CONFIRMED)
    _make_order(pv_earned=60, status=Order.Status.PENDING)
    _make_order(pv_earned=60, status=Order.Status.CANCELLED)
    _make_order(pv_earned=60, status=Order.Status.REFUNDED)

    assert get_retail_distributor_ratio() == Decimal("100.00")


# ---------------------------------------------------------------------------
# check_retail_ratio_and_alert
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
def test_fires_an_alert_when_the_ratio_drops_below_threshold(mock_send_mail):
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    _make_order(pv_earned=60)
    _make_order(pv_earned=60)
    _make_order(pv_earned=0)  # 33.33% retail, below the 70% default

    check_retail_ratio_and_alert()

    mock_send_mail.assert_called_once()
    kwargs = mock_send_mail.call_args.kwargs
    assert kwargs["recipient_list"] == ["compliance@bancostore.test"]
    state = ComplianceAlertState.objects.get(pk=1)
    assert state.is_below_threshold is True
    assert state.last_alert_sent_at is not None


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
def test_does_not_fire_when_the_ratio_is_above_threshold(mock_send_mail):
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    _make_order(pv_earned=0)
    _make_order(pv_earned=0)
    _make_order(pv_earned=0)
    _make_order(pv_earned=0)
    _make_order(pv_earned=60)  # 80% retail, above the 70% default

    check_retail_ratio_and_alert()

    mock_send_mail.assert_not_called()
    state, _ = ComplianceAlertState.objects.get_or_create(pk=1)
    assert state.is_below_threshold is False


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
def test_does_not_re_alert_on_every_order_while_still_below_threshold(mock_send_mail):
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    _make_order(pv_earned=60)
    _make_order(pv_earned=0)  # 50% retail, below threshold
    check_retail_ratio_and_alert()
    mock_send_mail.assert_called_once()

    _make_order(pv_earned=60)  # ratio drops further, still below threshold
    check_retail_ratio_and_alert()

    mock_send_mail.assert_called_once()  # still just the one call, not two


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
def test_fires_again_after_recovering_and_dropping_below_threshold_again(
    mock_send_mail,
):
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    _make_order(pv_earned=60)
    _make_order(pv_earned=0)  # 50% retail, below threshold
    check_retail_ratio_and_alert()
    mock_send_mail.assert_called_once()

    # Recover above threshold.
    for _ in range(10):
        _make_order(pv_earned=0)
    check_retail_ratio_and_alert()
    state = ComplianceAlertState.objects.get(pk=1)
    assert state.is_below_threshold is False

    # Drop below threshold again -- must fire a second, fresh alert.
    for _ in range(20):
        _make_order(pv_earned=60)
    check_retail_ratio_and_alert()

    assert mock_send_mail.call_count == 2


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
def test_is_a_noop_when_no_paid_orders_exist(mock_send_mail):
    check_retail_ratio_and_alert()  # must not raise

    mock_send_mail.assert_not_called()
    assert not ComplianceAlertState.objects.filter(pk=1).exists()


@pytest.mark.django_db
def test_logs_a_warning_instead_of_crashing_when_no_alert_email_is_configured(caplog):
    config.COMPLIANCE_ALERT_EMAIL = ""
    _make_order(pv_earned=60)
    _make_order(pv_earned=0)  # below threshold

    with caplog.at_level("WARNING"):
        check_retail_ratio_and_alert()  # must not raise

    assert "not configured" in caplog.text
    state = ComplianceAlertState.objects.get(pk=1)
    assert state.is_below_threshold is True
    assert state.last_alert_sent_at is None


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail", side_effect=Exception("SMTP down"))
def test_an_email_failure_does_not_raise(mock_send_mail):
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    _make_order(pv_earned=60)
    _make_order(pv_earned=0)  # below threshold

    check_retail_ratio_and_alert()  # must not raise

    state = ComplianceAlertState.objects.get(pk=1)
    assert state.is_below_threshold is True
    assert state.last_alert_sent_at is None  # send genuinely failed
