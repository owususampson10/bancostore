"""Task 63c. The admin is warned before SMS credit runs out, and told the
moment it has.

Found 2026-09-17: mNotify had 0 credits, and every SMS -- distributors'
login codes, customers' order texts -- had been failing with HTTP 402 while
nobody knew. The only trace was a traceback in the server's error log.
"""

from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import override_settings

import pytest
from constance import config

from apps.notifications.models import AdminNotification
from apps.notifications.sms import SmsOutOfCredit, SmsSendError, send_sms
from apps.notifications.sms_alerts import (
    ALERT_RETRY_SECONDS,
    OUT_OF_CREDIT_ALERT_KEY,
    alert_admin_about_sms_credit,
)
from apps.notifications.tasks import check_sms_credit


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def alert_email():
    config.SMS_CREDIT_ALERT_EMAIL = "ops@bancostore.test"


def test_the_warning_level_defaults_to_200_credits():
    """User-confirmed 2026-09-17."""
    from apps.platform_settings.config import NOTIFICATION_AND_COMMUNICATION_SETTINGS

    assert NOTIFICATION_AND_COMMUNICATION_SETTINGS["SMS_LOW_CREDIT_THRESHOLD"][0] == 200


# --- The alert itself -------------------------------------------------------


@pytest.mark.django_db
def test_low_credit_emails_the_admin_and_rings_the_bell(locmem, alert_email):
    alert_admin_about_sms_credit(credits=150)

    (message,) = mail.outbox
    assert message.to == ["ops@bancostore.test"]
    assert "150" in message.body
    bell = AdminNotification.objects.get()
    assert bell.event_type == AdminNotification.EventType.SMS_CREDIT
    assert "150" in bell.message


@pytest.mark.django_db
def test_running_out_says_texts_are_failing(locmem, alert_email):
    alert_admin_about_sms_credit(credits=0)

    (message,) = mail.outbox
    assert "run out" in message.subject.lower()
    assert "run out" in AdminNotification.objects.get().message.lower()


@pytest.mark.django_db
def test_a_low_credit_alert_is_sent_at_most_once_a_day(locmem, alert_email):
    """The check runs hourly; nagging every hour would train the admin to
    ignore it."""
    alert_admin_about_sms_credit(credits=150)
    alert_admin_about_sms_credit(credits=140)

    assert len(mail.outbox) == 1
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_running_out_is_announced_even_after_a_low_credit_warning(locmem, alert_email):
    """ "Low" and "gone" are different news: the second must not be
    swallowed by the first one's once-a-day limit."""
    alert_admin_about_sms_credit(credits=150)
    alert_admin_about_sms_credit(credits=0)

    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_without_an_alert_email_only_the_bell_rings(locmem):
    config.SMS_CREDIT_ALERT_EMAIL = ""

    alert_admin_about_sms_credit(credits=0)

    assert mail.outbox == []
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_a_failing_email_never_escapes_and_the_bell_still_rings(alert_email):
    with patch(
        "apps.notifications.sms_alerts.send_mail", side_effect=OSError("smtp down")
    ):
        alert_admin_about_sms_credit(credits=0)  # must not raise

    assert AdminNotification.objects.count() == 1


# --- Told the moment a send fails for lack of credit ------------------------


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY="test-key")
def test_a_send_refused_for_lack_of_credit_alerts_the_admin(locmem, alert_email):
    response = type("R", (), {"status_code": 402})()
    with (
        patch("apps.notifications.sms.requests.post", return_value=response),
        pytest.raises(SmsOutOfCredit),
    ):
        send_sms("+233241234567", "Hello")

    (message,) = mail.outbox
    assert "run out" in message.subject.lower()


@pytest.mark.django_db
@override_settings(MNOTIFY_API_KEY="test-key")
def test_a_broken_alert_never_hides_the_send_failure():
    """The caller must still learn the SMS failed (the OTP page relies on
    it), even if alerting the admin blows up."""
    response = type("R", (), {"status_code": 402})()
    with (
        patch("apps.notifications.sms.requests.post", return_value=response),
        # The alert guards its own email and bell, so break the alert as a
        # whole -- that is what the guard in sms.py exists for.
        patch(
            "apps.notifications.sms_alerts.alert_admin_about_sms_credit",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(SmsOutOfCredit),
    ):
        send_sms("+233241234567", "Hello")


# --- The hourly check -------------------------------------------------------


@pytest.mark.django_db
def test_the_hourly_check_warns_below_the_threshold(locmem, alert_email):
    config.SMS_LOW_CREDIT_THRESHOLD = 200

    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=199):
        check_sms_credit()

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_the_hourly_check_is_quiet_at_or_above_the_threshold(locmem, alert_email):
    config.SMS_LOW_CREDIT_THRESHOLD = 200

    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=200):
        check_sms_credit()

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_top_up_resets_the_warning(locmem, alert_email):
    """After credit is topped up, the next drop must warn straight away,
    not wait out the previous day's limit."""
    config.SMS_LOW_CREDIT_THRESHOLD = 200

    for balance in (150, 5000, 150):
        with patch(
            "apps.notifications.tasks.get_sms_credit_balance", return_value=balance
        ):
            check_sms_credit()

    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_a_zero_threshold_turns_off_only_the_early_warning(locmem, alert_email):
    config.SMS_LOW_CREDIT_THRESHOLD = 0

    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=150):
        check_sms_credit()
    assert mail.outbox == []

    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=0):
        check_sms_credit()
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_the_hourly_check_skips_quietly_when_there_is_no_provider(locmem, alert_email):
    with patch("apps.notifications.tasks.get_sms_credit_balance", return_value=None):
        check_sms_credit()

    assert mail.outbox == []


@pytest.mark.django_db
def test_an_unreadable_balance_is_logged_not_raised(locmem, alert_email, caplog):
    with patch(
        "apps.notifications.tasks.get_sms_credit_balance",
        side_effect=SmsSendError("could not reach mNotify"),
    ):
        check_sms_credit()  # must not raise

    assert mail.outbox == []
    assert "could not read" in caplog.text


@pytest.mark.django_db
def test_the_hourly_check_is_scheduled():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.get(name="check-sms-credit")
    assert task.task == "apps.notifications.tasks.check_sms_credit"
    assert task.interval.every == 60
    assert task.interval.period == "minutes"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bell_failure",
    [
        # How send_admin_notification really fails: it returns None rather
        # than raising (agent review, PR #94).
        {"return_value": None},
        {"side_effect": RuntimeError("down")},
    ],
    ids=["bell-returns-none", "bell-raises"],
)
def test_when_every_channel_fails_the_alert_is_tried_again(
    locmem, alert_email, bell_failure
):
    """CodeRabbit (PR #94). The once-a-day limit must only start once an
    admin was actually told; otherwise one bad moment silences a day. The
    retry waits a few minutes, though (agent review): every failed send
    triggers this inline, and trying again on each one would add an
    email-timeout wait to every send during an outage."""
    with (
        patch("apps.notifications.sms_alerts.send_mail", side_effect=OSError("down")),
        patch("apps.notifications.sms_alerts.send_admin_notification", **bell_failure),
    ):
        alert_admin_about_sms_credit(credits=0)
        alert_admin_about_sms_credit(credits=0)  # the next failed send, moments later

    ttl = cache.ttl(OUT_OF_CREDIT_ALERT_KEY)
    assert 0 < ttl <= ALERT_RETRY_SECONDS  # retried soon, not tomorrow

    cache.delete(OUT_OF_CREDIT_ALERT_KEY)  # those few minutes have passed
    alert_admin_about_sms_credit(credits=0)  # everything works again

    assert len(mail.outbox) == 1
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_one_working_channel_is_enough_to_start_the_daily_limit(locmem, alert_email):
    with patch("apps.notifications.sms_alerts.send_mail", side_effect=OSError("down")):
        alert_admin_about_sms_credit(credits=0)  # bell rings, email fails

    alert_admin_about_sms_credit(credits=0)

    assert AdminNotification.objects.count() == 1  # not rung a second time


@pytest.mark.django_db
def test_a_failed_email_and_a_bell_that_returns_nothing_starts_no_daily_limit(
    locmem, alert_email
):
    """Agent review (PR #94): the real bell signals failure by returning
    None. Treating any return as success would silence alerts for a day."""
    with (
        patch("apps.notifications.sms_alerts.send_mail", side_effect=OSError("down")),
        patch(
            "apps.notifications.sms_alerts.send_admin_notification", return_value=None
        ),
    ):
        alert_admin_about_sms_credit(credits=0)

    assert cache.ttl(OUT_OF_CREDIT_ALERT_KEY) <= ALERT_RETRY_SECONDS
