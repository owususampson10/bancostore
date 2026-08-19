# ADR-0013: Production incident — django-constance pickle→JSON serialization mismatch

## Status

Accepted (2026-08-19)

## Date

2026-08-19

## Context

While deploying PR #83 (an unrelated dead-links/admin-password-reset fix, no migrations, no
`requirements.txt` change) to production, restarting the three Supervisor-managed processes
(`bancostore-daphne`, `bancostore-celery-worker`, `bancostore-celery-beat`) — a routine step in
`deploy/README.md`'s own runbook — caused the storefront home page and shop page to start
returning HTTP 500. Distributor/admin login and other non-catalog pages stayed up throughout.

### Root cause

Task 51 (PR #82, merged earlier) bumped `django-constance` from `>=3,<4` to `>=4.3,<5` — a major
version bump — to clear pip-audit findings. Starting in the 4.x line, constance's database backend
serializes stored setting values as JSON (`constance/codecs.py`, a `{"__type__": ..., "__value__":
...}` envelope). Versions before that serialized values as base64-encoded Python `pickle` (the
`constance[database]` backend's older format). **This is a stored-data format change, not just a
code change** — and nothing re-encoded the 81 already-stored rows in production's
`constance_constance` table when the library was upgraded. Every single row was still in the old
pickle format.

This was invisible for as long as it was, because `pip install -r requirements.txt` upgrading the
on-disk package does nothing to processes that already have the *old* module imported into memory
— Python caches imports in `sys.modules` for a process's entire lifetime. Whichever deploy actually
ran `pip install` for the constance bump did not fully restart-and-verify the Supervisor programs
afterward (or did, but never checked the actual storefront response, not just `supervisorctl
status`, which shows `RUNNING` regardless of whether requests inside that process are erroring).
The already-running process kept serving traffic correctly using the *old* pickle-compatible
codec in memory, with no symptom at all — until this deploy's restart forced a fresh process to
import the *new* constance 4.3 code, which tried to `json.loads()` a base64-pickle string and
raised `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` inside
`Config.__init__()`'s `autofill()` — the very first constance value read in that process's life,
which happens on essentially every page (`apps/catalog/services.py::storefront_visible_products`
was the first call site to trip it, since it's on the home/shop path; any other view reading any
constance value in a fresh process would have hit the identical failure).

### Why this was hard to catch beforehand

- `django-constance`'s own migrations only manage the `constance_constance` table's *schema*
  (the `key`/`value` columns), never its *contents* — a version bump changing how `value` is
  encoded ships no data migration, because the library has no way to know what's already stored.
- `pip-audit` (the reason for the bump) only checks for known vulnerabilities in the *installed*
  version; it says nothing about serialization-format compatibility with already-persisted data.
- CI and local dev both start from a fresh database (migrations + fixtures/seed commands), so this
  category of bug — old data encoded by an old library version, read by a new one — has no way to
  surface in either environment. It can only be caught by testing against a real, previously-seeded
  production-shaped database, which this project doesn't have in CI.

## Decision

### 1. Fix applied: a one-time, verified data migration on the live `constance_constance` table

For each of the 81 rows: `base64.b64decode(value)` → `pickle.loads(...)` (trusted, self-generated
data — every row was written by this project's own earlier constance version, never external
input) → re-encode with the *current* `constance.codecs.dumps()` → save. Run first as a dry run
(print every recovered value without writing), confirmed all 81 values matched this project's
documented seeded defaults exactly (e.g. `MAX_FAILED_LOGIN_ATTEMPTS = 5`,
`REGISTRATION_FEE = Decimal('100')`, `CURRENCY = 'GHS'`) with zero decode failures, then applied
for real inside one `transaction.atomic()` block, followed by `cache.clear()` (Redis, in case a
stale entry existed there too) and a final Supervisor restart. Verified via `curl -I` against
every major route (home, shop, login, about, contact, the new admin password-reset page) and a
clean error-log tail after the fix — no data loss, ~2–3 minutes of storefront-only impact.

### 2. Process fix: `deploy/README.md`'s runbook gets an explicit dependency-upgrade step

Added a new checklist item (see the runbook itself): **any deploy that changes `requirements.txt`
must restart all three Supervisor programs as part of that same deploy session — never merged in
one session and restarted "later" or in a separate session** — and must curl-verify at least the
home page (not just `supervisorctl status`) immediately after. The specific trap this closes: a
dependency upgrade can sit fully "deployed" (code pulled, `pip install` run) while the actual
running processes silently keep using the old in-memory version indefinitely, deferring the real
verification of the upgrade to whatever *unrelated* future deploy happens to be the next one that
restarts services — exactly what happened here.

## Alternatives Considered

### Revert the django-constance upgrade instead of fixing the data

Rejected: the upgrade itself was correct and already-merged (it fixed real pip-audit findings);
the actual bug is un-migrated data, not the library choice. Reverting would leave the pip-audit
findings unfixed again and still require this exact same data fix whenever the upgrade is
eventually redone.

### Write a formal Django data migration for the re-encoding instead of a one-off shell script

Considered for auditability, but rejected as unnecessary process for a single trusted one-time
production data-repair — this project's `apps/*/migrations/` are for schema + seed data, not
ad-hoc incident remediation. Documented here (this ADR) instead, matching how other one-off
production fixes in this project's history have been handled.

## Consequences

- All 81 constance values are now stored in the correct JSON format; any future constance library
  upgrade in the same major-version-bump-changes-serialization shape will need this same
  dry-run-then-fix treatment, now with this ADR as a precedent to follow rather than rediscover.
- `deploy/README.md`'s runbook is the durable fix — it directly targets the actual gap (a
  dependency upgrade whose restart-and-verify step got silently deferred), not just this one
  incident's symptom.
- This incident was caught only because this deploy happened to include a routine service
  restart and the deployer (this session) checked real HTTP responses, not just process status —
  underscoring that `supervisorctl status: RUNNING` is not sufficient deploy verification on its
  own, for this class of bug specifically.
