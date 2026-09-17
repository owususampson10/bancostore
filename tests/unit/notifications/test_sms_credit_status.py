"""Task 66. The site remembers the last known SMS credit balance, so every
admin page can decide whether to show the credit banner without asking
mNotify on each page load.

A "shortage" starts when credit first falls below the warning level (or to
0) and ends when it is back at or above it. Each shortage gets its own start
time, so a banner the admin closed during one shortage comes back for the
next one.
"""

from unittest.mock import MagicMock, patch

from django.test import override_settings

import pytest
from constance import config

from apps.notifications.models import SmsCreditStatus
from apps.notifications.sms import send_sms
from apps.notifications.sms_credit_status import credit_banner, record_sms_credit
from apps.notifications.tasks import check_sms_credit


@pytest.fixture(autouse=True)
def threshold():
    config.SMS_LOW_CREDIT_THRESHOLD = 200


@pytest.mark.django_db
def test_nothing_is_known_until_the_first_check():
    assert credit_banner() is None


@pytest.mark.django_db
def test_low_credit_gives_an_amber_low_banner():
    record_sms_credit(150)

    banner = credit_banner()
    assert banner.kind == "low"
    assert banner.credits == 150


@pytest.mark.django_db
def test_no_credit_gives_a_red_out_banner():
    record_sms_credit(0)

    assert credit_banner().kind == "out"


@pytest.mark.django_db
def test_healthy_credit_gives_no_banner():
    record_sms_credit(200)

    assert credit_banner() is None


@pytest.mark.django_db
def test_a_zero_threshold_still_shows_the_out_banner_only():
    config.SMS_LOW_CREDIT_THRESHOLD = 0

    record_sms_credit(5)
    assert credit_banner() is None

    record_sms_credit(0)
    assert credit_banner().kind == "out"


@pytest.mark.django_db
def test_a_shortage_keeps_its_identity_while_it_lasts():
    """Dropping further within one shortage is the same shortage, so a
    closed banner stays closed while credit keeps draining."""
    record_sms_credit(150)
    first = credit_banner().shortage_id

    record_sms_credit(90)

    assert credit_banner().shortage_id == first


@pytest.mark.django_db
def test_a_new_shortage_after_a_top_up_gets_a_new_identity():
    record_sms_credit(150)
    first = credit_banner().shortage_id

    record_sms_credit(1600)  # topped up
    record_sms_credit(150)  # fell below again later

    assert credit_banner().shortage_id != first


@pytest.mark.django_db
def test_there_is_only_ever_one_status_row():
    for credits in (150, 0, 1600):
        record_sms_credit(credits)

    assert SmsCreditStatus.objects.count() == 1


# --- Where the balance comes from -------------------------------------------


@pytest.mark.django_db
def test_the_hourly_check_records_the_balance():
    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=150):
        check_sms_credit()

    assert credit_banner().credits == 150


@pytest.mark.django_db
def test_a_failed_balance_read_keeps_the_last_known_state():
    from apps.notifications.sms import SmsSendError

    record_sms_credit(150)
    with patch(
        "apps.notifications.tasks.get_sms_credit_balance",
        side_effect=SmsSendError("down"),
    ):
        check_sms_credit()

    assert credit_banner().credits == 150


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY="test-key")
def test_a_send_refused_for_lack_of_credit_records_zero():
    """The banner turns red the moment a text fails for lack of credit,
    not up to an hour later at the next check."""
    with (
        patch(
            "apps.notifications.sms.requests.post",
            return_value=MagicMock(status_code=402),
        ),
        pytest.raises(Exception),
    ):
        send_sms("+233241234567", "Hello")

    assert credit_banner().kind == "out"


@pytest.mark.django_db
def test_recording_never_raises():
    """It is called from inside failing SMS sends and the hourly job."""
    with patch.object(
        SmsCreditStatus.objects, "update_or_create", side_effect=RuntimeError
    ):
        record_sms_credit(0)  # must not raise
