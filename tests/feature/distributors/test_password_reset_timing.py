"""Task 65. The password reset pages must not reveal, by how long they take,
whether a phone number has an account.

Before this, forgot_password and resend (password reset) looked the number
up and, for a real account, texted the code during the request -- roughly a
second talking to mNotify. An unknown number skipped both and answered
faster, so timing the page hinted at which numbers are registered (CWE-208;
CodeRabbit, PR #94). The page now does the same, constant work for every
number: it queues a background job. The lookup and the send happen in the
worker, where nobody can time them.
"""

import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor

User = get_user_model()
KNOWN = "+233241234567"
UNKNOWN = "+233249999999"


def _distributor(phone=KNOWN, email="kofi@example.com"):
    user = User.objects.create_user(
        username=phone, password="Passw0rd-123!", email=email
    )
    group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(group)
    return Distributor.objects.create(
        user=user, phone_number=phone, phone_verified=True
    )


def _query_shapes(captured):
    """Each query with its literal values blanked, so two requests compare
    by what they ask the database, not by session keys, timestamps or the
    phone number typed. The request profiler's own bookkeeping rows are
    dropped: they record every request, whatever it does."""
    shapes = []
    for query in captured:
        sql = query["sql"]
        if "silk_" in sql:
            continue
        # Numbered savepoint names: "s1_x2" on SQLite, `s1_x2` on MySQL (CI).
        sql = re.sub(r'["`]s\d+_x\d+["`]', "?", sql)
        sql = re.sub(r"'(?:[^']|'')*'", "?", sql)
        sql = re.sub(r"\b\d+(?:\.\d+)?\b", "?", sql)
        shapes.append(sql)
    return shapes


def _set_reset_session(client, phone):
    session = client.session
    session["otp_phone_number"] = phone
    session["otp_purpose"] = "password_reset"
    session.save()


def _forgot(client, phone):
    return client.post(reverse("distributors:forgot_password"), {"phone_number": phone})


def _resend(client, phone):
    _set_reset_session(client, phone)
    return client.post(reverse("distributors:resend_otp"))


@pytest.fixture
def queued():
    with patch("apps.distributors.views.send_password_reset_code_task.delay") as delay:
        yield delay


@pytest.mark.django_db
@pytest.mark.parametrize("post", [_forgot, _resend], ids=["forgot", "resend"])
def test_known_and_unknown_numbers_run_identical_queries(client, queued, post):
    """The strongest check available without a stopwatch: the request runs
    the same database queries whether or not the number has an account.
    Creating a code, or anything else done only for real accounts, shows up
    as extra queries for one of them. (A bare lookup that runs identically
    for both would not -- and takes the same microseconds either way, so it
    isn't the leak. The ~1 second mNotify call is, and the next test pins
    that nothing is sent during the request.)"""
    _distributor()

    captured = {}
    for phone in (KNOWN, UNKNOWN):
        client.cookies.clear()
        with CaptureQueriesContext(connection) as queries:
            response = post(client, phone)
        assert response.status_code == 302
        captured[phone] = _query_shapes(queries.captured_queries)

    assert captured[KNOWN] == captured[UNKNOWN]


@pytest.mark.django_db
@pytest.mark.parametrize("post", [_forgot, _resend], ids=["forgot", "resend"])
def test_every_number_is_queued_and_nothing_is_sent_during_the_request(
    client, queued, post
):
    _distributor()

    with (
        patch("apps.notifications.otp.send_sms") as send_sms,
        patch("apps.notifications.otp.send_mail") as send_mail,
    ):
        post(client, KNOWN)
        client.cookies.clear()
        post(client, UNKNOWN)

    assert [c.args for c in queued.call_args_list] == [(KNOWN,), (UNKNOWN,)]
    send_sms.assert_not_called()
    send_mail.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("post", [_forgot, _resend], ids=["forgot", "resend"])
def test_a_broker_outage_still_answers_the_same_way(client, post):
    """If the job can't be queued, the visitor still gets the usual page --
    an error only for real accounts would bring the hint straight back."""
    _distributor()

    with patch(
        "apps.distributors.views.send_password_reset_code_task.delay",
        side_effect=ConnectionError("Redis is down"),
    ):
        response = post(client, KNOWN)

    assert response.status_code == 302
    assert response.url == reverse("distributors:verify_otp")


# --- The background job -----------------------------------------------------


@pytest.mark.django_db
def test_the_job_sends_a_code_to_a_real_account():
    from apps.distributors.tasks import send_password_reset_code_task
    from apps.notifications.sms import fake_outbox

    _distributor()

    send_password_reset_code_task(KNOWN)

    assert fake_outbox and fake_outbox[-1]["phone_number"] == KNOWN


@pytest.mark.django_db
def test_the_job_sends_nothing_for_an_unknown_number():
    from apps.distributors.tasks import send_password_reset_code_task
    from apps.notifications.models import OTPCode
    from apps.notifications.sms import fake_outbox

    send_password_reset_code_task(UNKNOWN)

    assert fake_outbox == []
    assert not OTPCode.objects.filter(phone_number=UNKNOWN).exists()


@pytest.mark.django_db
def test_the_job_never_raises_when_the_code_cannot_be_delivered():
    """A failed delivery is already logged by generate_otp; raising would
    only make Celery report an error for something nobody can fix by
    retrying the same job."""
    from apps.distributors.tasks import send_password_reset_code_task
    from apps.notifications.sms import SmsOutOfCredit

    _distributor(email="")

    with patch("apps.notifications.otp.send_sms", side_effect=SmsOutOfCredit("x")):
        send_password_reset_code_task(KNOWN)  # must not raise
