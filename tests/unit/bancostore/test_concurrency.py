from django.db import OperationalError

import pytest

from bancostore.concurrency import retry_on_lock_contention


def test_returns_the_function_result_on_first_success():
    result = retry_on_lock_contention(lambda: "ok")

    assert result == "ok"


def test_retries_a_bounded_number_of_times_on_lock_contention():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OperationalError("database table is locked")
        return "eventually ok"

    result = retry_on_lock_contention(flaky, backoff_seconds=0)

    assert result == "eventually ok"
    assert len(calls) == 3


def test_gives_up_and_reraises_after_max_retries():
    def always_locked():
        raise OperationalError("database is locked")

    with pytest.raises(OperationalError):
        retry_on_lock_contention(always_locked, max_retries=3, backoff_seconds=0)


def test_recognizes_mysql_lock_wait_timeout_and_deadlock_messages():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("(1205, 'Lock wait timeout exceeded')")
        if len(calls) == 2:
            raise OperationalError("(1213, 'Deadlock found')")
        return "ok"

    result = retry_on_lock_contention(flaky, backoff_seconds=0)

    assert result == "ok"
    assert len(calls) == 3


def test_does_not_retry_a_non_lock_operational_error():
    def unrelated_error():
        raise OperationalError("no such table: foo")

    with pytest.raises(OperationalError):
        retry_on_lock_contention(unrelated_error, max_retries=5, backoff_seconds=0)


def test_a_non_operational_exception_propagates_immediately_without_retrying():
    calls = []

    def business_error():
        calls.append(1)
        raise ValueError("insufficient stock")

    with pytest.raises(ValueError):
        retry_on_lock_contention(business_error, max_retries=5, backoff_seconds=0)

    assert len(calls) == 1
