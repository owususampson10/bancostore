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

select_for_update_nowait_if_supported() matters just as much as the retry
loop: without NOWAIT, a blocked select_for_update() on MySQL waits up to
innodb_lock_wait_timeout (50s by default) before it even raises the error
this module retries on. A handful of concurrent requests against the same
row could each tie up a worker thread for tens of seconds — a real
denial-of-service vector against the whole site's limited worker pool, not
just the contended endpoint. Found in review, not hypothetical: a fresh
audit of this exact helper flagged it (bancostore/concurrency.py's own
retry loop, layered on top of a non-NOWAIT lock, was the mechanism).
"""

import time

from django.db import OperationalError, connection

MAX_LOCK_RETRIES = 10
RETRY_BACKOFF_SECONDS = 0.05


def _is_lock_contention_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "is locked" in message  # SQLite: "database is locked" /
        # "database table is locked" — matches either without guessing
        # which exact phrasing a given SQLite version uses.
        or "lock wait timeout" in message  # MySQL: blocking wait exceeded
        or "deadlock" in message  # MySQL
        or "could not be acquired" in message  # MySQL: NOWAIT hit a held lock
    )


def select_for_update_nowait_if_supported(queryset):
    """Use NOWAIT when the backend supports it (MySQL/Postgres) so lock
    contention fails immediately instead of blocking. SQLite doesn't
    support NOWAIT at all — Django raises NotSupportedError if nowait=True
    is passed on a backend without has_select_for_update_nowait — so this
    only enables it where it's actually safe to. On SQLite this is
    equivalent to a plain select_for_update() (itself a no-op there; see
    the module docstring)."""
    nowait = connection.features.has_select_for_update_nowait
    return queryset.select_for_update(nowait=nowait)


def retry_on_lock_contention(
    fn,
    *,
    max_retries: int = MAX_LOCK_RETRIES,
    backoff_seconds: float = RETRY_BACKOFF_SECONDS,
):
    """Call fn() — which should wrap its own transaction.atomic() +
    select_for_update_nowait_if_supported() — retrying a bounded number of
    times if the database raises a lock-contention error. Any other
    exception (e.g. a domain-level error like InsufficientStockError)
    propagates immediately without retrying, since retrying wouldn't
    change a legitimate business outcome.

    Combined with NOWAIT, each failed attempt is fast (no multi-second
    block), so max_retries * backoff_seconds bounds worst-case added
    latency to a fraction of a second, not tens of seconds per attempt."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except OperationalError as exc:
            if attempt >= max_retries or not _is_lock_contention_error(exc):
                raise
            time.sleep(backoff_seconds * attempt)
