"""Shared helper for read-modify-write DB operations that must survive real
concurrent access — select_for_update() alone isn't enough. Real concurrent
writes raise a raw OperationalError while a competing transaction holds the
row lock ("database is/table is locked" on SQLite, lock-wait-timeout or
deadlock on MySQL) rather than blocking gracefully in every case. This
wasn't theoretical: a genuine multi-threaded test against local SQLite
reproduced it directly for apps/catalog/services.py::decrement_stock (see
tests/feature/catalog/test_stock.py::
test_concurrent_decrements_never_oversell_the_last_unit) before this helper
existed. The same pattern applies to any bounded counter guarded by a lock
— admin/distributor failed-login counters included.
"""

import time

from django.db import OperationalError

MAX_LOCK_RETRIES = 10
RETRY_BACKOFF_SECONDS = 0.05


def _is_lock_contention_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "is locked" in message  # SQLite: "database is locked" /
        # "database table is locked" — matches either without guessing
        # which exact phrasing a given SQLite version uses.
        or "lock wait timeout" in message  # MySQL
        or "deadlock" in message  # MySQL
    )


def retry_on_lock_contention(
    fn,
    *,
    max_retries: int = MAX_LOCK_RETRIES,
    backoff_seconds: float = RETRY_BACKOFF_SECONDS,
):
    """Call fn() — which should wrap its own transaction.atomic() +
    select_for_update() — retrying a bounded number of times if the
    database raises a lock-contention error. Any other exception (e.g. a
    domain-level error like InsufficientStockError) propagates immediately
    without retrying, since retrying wouldn't change a legitimate business
    outcome."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except OperationalError as exc:
            if attempt >= max_retries or not _is_lock_contention_error(exc):
                raise
            time.sleep(backoff_seconds * attempt)
