# ADR-0003: Binary Bonus carry-forward — concurrency, consumption, and rounding design

## Status

Accepted (2026-07-15)

## Date

2026-07-15

## Context

Task 13 (Binary Bonus) requires, every payout cycle: find each distributor's weak leg PV, pay
`BINARY_BONUS_RATE` x weak-leg-PV (capped by `WEEKLY_BINARY_BONUS_CAP`), and carry the strong leg's
excess forward, expiring PV after `PV_CARRY_FORWARD_EXPIRY_DAYS`. Unlike Task 12's Direct Referral
Bonus (a single instant credit with no state to consume), this requires a genuinely new pattern:
dated, per-leg PV buckets (`PvDailyBucket`) that get read, partially consumed, and expired on a
recurring schedule — while purchases keep crediting the same rows concurrently from a completely
separate code path (`record_purchase_pv`).

Three design questions had no obvious answer and were resolved through `doubt-driven-development`
(3 review rounds on the write-time bucket-crediting mechanism, then a 4th round plus a follow-up
code review on the read/consume cycle itself) rather than being derived from `SPEC.md`, which
doesn't address any of them:

1. **How to bulk-decrement dozens of dated buckets per leg without a per-row loop or raw SQL**, matching
   this project's existing "bulk `F()` update, not per-ancestor" convention (`record_purchase_pv`).
2. **How to make the whole cycle (expire + sum + pay + consume) safe against concurrent purchases
   crediting the SAME buckets mid-cycle, and against the cycle itself being retried or double-run**,
   without introducing per-bucket row locking, which the write path's own review already flagged as
   architecturally risky (see `apps/pv_ledger/services.py::_credit_daily_buckets`'s own history —
   a real, review-missed concurrency bug was only caught by a genuine 10-thread test, not reasoning).
3. **What to do with the PV behind a bonus the weekly cap partially blocks** — the client-facing
   version of this question ("what happens to the PV that produced the disallowed excess?") was
   answered by the user directly: it must not be silently discarded. But the exact mechanics
   (how much PV to actually mark "spent" when only part of the raw bonus gets paid) required a
   concrete rounding rule, not just the "don't lose it" principle.

## Decision

### 1. Bulk consumption via `Case`/`When`, not a per-bucket loop

`consume_leg_pv_fifo` (`apps/pv_ledger/services.py`) computes, in Python, which buckets (oldest
date first) get fully or partially decremented, then issues **one** bulk UPDATE per leg:

```python
case_expr = Case(
    *[When(pk=pk, then=F("pv") - take) for pk, take in decrements.items()],
    default=F("pv"),
)
PvDailyBucket.objects.filter(pk__in=list(decrements.keys())).update(pv=case_expr)
```

Every branch is `F()`-relative (never a literal computed value), so the decrement is always applied
to whatever a row's live value is at UPDATE time — safe even if a concurrent purchase incremented
that same row moments earlier. This is one SQL statement per leg regardless of how many dated
buckets are touched (bounded by `PV_CARRY_FORWARD_EXPIRY_DAYS`, ~180 max), matching
`record_purchase_pv`'s existing "two bulk statements, never one per ancestor" precedent. Still pure
Django ORM — no raw SQL.

### 2. Lock the Distributor row, not `PvDailyBucket` rows; rely on a documented monotonicity invariant

`process_binary_bonus_for_distributor` (`apps/commissions/services.py`) locks the `Distributor` row
first, inside the atomic block, before even checking whether this cycle already ran:

```python
select_for_update_nowait_if_supported(
    Distributor.objects.filter(pk=distributor.pk)
).get()
```

This serializes any concurrent or retried invocation for the *same distributor's cycle* — a second
attempt blocks (or gets a contention error `retry_on_lock_contention` retries) until the first's
transaction fully commits or rolls back, then sees the first's already-committed
`WalletTransaction` and returns early. It does **not** lock `PvDailyBucket` rows directly, and
deliberately doesn't need to: an earlier draft tried `select_for_update()` combined with
`.aggregate(Sum(...))` on the bucket rows and a fresh review couldn't confirm this combination's
behavior was actually verified against Django's documented guarantees, so it was dropped rather than
shipped on faith.

Correctness instead rests on one invariant, verified against the actual write path and proven with
a real threaded test, not just reasoned about: **`PvDailyBucket.pv` is only ever increased outside
this function (by `_credit_daily_buckets`, the write-time purchase-credit path) and only ever
decreased inside it.** A concurrent purchase landing mid-cycle can therefore only make *more* PV
available than the cycle's `sum_leg_pv` already counted, never less — and `consume_leg_pv_fifo`
structurally never consumes more than what was counted, so it can never reach into (or lose) PV a
concurrent purchase just added. `test_a_concurrent_purchase_write_mid_cycle_never_loses_pv`
precisely synchronizes a real purchase write to land exactly after the cycle's totals are read,
proving this empirically rather than trusting the invariant's math alone — the same lesson learned
the hard way earlier in Task 13, when a "the math checks out" design for the write path shipped
with a real, silent PV-loss bug that only a genuine 10-thread test caught.

### 3. Floor (never round) the PV consumed under the weekly cap; defer rather than pay a sub-1-PV sliver

When `apply_weekly_binary_bonus_cap` reduces the payout below the raw calculated bonus, only the
proportional share of weak-leg PV that was *actually monetized* is consumed:

```python
pv_to_consume = int(weak_leg_pv * actual_bonus / raw_bonus)  # floor, never round up
```

The remainder stays in the buckets, uncontested, for a future cycle (subject to its own expiry
clock, unaffected by this cycle running) — this is the direct fix for the client-facing gap: PV
behind a capped-out excess is preserved, not discarded. Flooring (rather than rounding to nearest)
is deliberate: rounding up would mark PV as "spent" that was never actually paid for, a real
money-leak direction the user explicitly ruled out.

If `actual_bonus > 0` but floors to `pv_to_consume == 0` (the cap left a nonzero but sub-1-PV sliver
of room), **nothing is paid and nothing is consumed this cycle** — the alternative (pay the sliver,
consume 0 PV) would leave the same weak-leg PV available to generate *further* slivers in later
cycles, a real double-pay risk against PV that was never marked spent. This is logged
(`"floors to 0 PV"`) so it's distinguishable in production from the ordinary "nothing owed" case,
per a follow-up review finding.

## Alternatives Considered

### Raw SQL `INSERT ... ON DUPLICATE KEY UPDATE` / vendor-branched upsert for bucket consumption

- Pros: A true single-statement upsert, no Python-side branch construction.
- Rejected: introduces a brand-new pattern (raw SQL, `connection.vendor` branching) this codebase
  has never needed anywhere else; the ORM-native `Case`/`When` achieves the same bulk, single-
  statement, per-leg update with zero new attack surface or cross-database syntax divergence.

### Per-ancestor / per-bucket loop for consumption

- Pros: Simplest to write and reason about line-by-line.
- Rejected: reintroduces the exact O(bucket-count) query-per-row anti-pattern this project's Scale
  Architecture (`SPEC.md`) already eliminated once for the write path; a fresh review flagged this
  specifically as a regression against that precedent.

### `select_for_update()` on `PvDailyBucket` rows instead of the `Distributor` row

- Pros: Locks the actual rows being read/written, which reads as more directly targeted.
- Rejected: combining `select_for_update()` with `.aggregate(Sum(...))` across a variable, growing
  set of dated rows was flagged by review as unverified against Django's actual documented
  behavior. Checked directly against Django 5.0's official `select_for_update()` reference
  (`https://docs.djangoproject.com/en/5.0/ref/models/querysets/#select-for-update`) as part of a
  later `source-driven-development` pass: the documented restrictions cover nullable relations,
  autocommit mode, and per-backend option support (`nowait`/`skip_locked`/`no_key`), but say
  nothing about combining it with `.aggregate()` or `.annotate()` either way — genuinely
  undocumented, not merely unfamiliar. Avoiding it was the right call per that same skill's own
  rule ("if you cannot find documentation for a pattern, say so explicitly" rather than assume it's
  safe), not an overcautious guess. The `Distributor`-row lock achieves full serialization of the
  property that actually matters (one cycle run per distributor at a time) with a single, simple,
  already-proven lock primitive this codebase already uses elsewhere
  (`consume_paid_starter_pack`).

### Round the capped PV consumption to the nearest whole PV, or always pay any nonzero capped amount

- Pros: Never defers a legitimately-owed payment, however small.
- Rejected: rounding up (or paying a sub-1-PV sliver while consuming 0 PV) both risk marking PV as
  spent that wasn't paid for, or paying repeatedly against PV that's never actually decremented —
  both are money-leak directions the user explicitly said must not happen. Flooring plus deferral
  is the only option that can't leak in either direction.

## Consequences

- **The write path (`_credit_daily_buckets`) and the read/consume path
  (`process_binary_bonus_for_distributor`) rely on complementary, not identical, concurrency
  strategies** — the write path uses its own bounded IntegrityError retry (a genuinely different
  hazard, a unique-key race on row creation) plus deference to `retry_on_lock_contention` for lock
  contention; the read path uses a `Distributor`-row lock plus a documented monotonicity invariant.
  A future contributor should not assume these are interchangeable or that "just add
  `select_for_update()` here too" is automatically the safe move without re-verifying the specific
  hazard being guarded against.
- **The monotonicity invariant (`PvDailyBucket.pv` only increases outside, only decreases inside
  `process_binary_bonus_for_distributor`) is enforced entirely by code discipline, not the schema.**
  Any future code path that writes to `PvDailyBucket.pv` outside these two functions must be
  reviewed against this invariant explicitly — it is not something a migration or constraint
  currently guards against.
- **A distributor can see a cycle where they're eligible, have a nonzero weak leg, but receive
  nothing** — either because the cap is fully exhausted, or because the cap-allowed room floors to
  0 PV. Both are correct, not bugs; support/admin tooling surfacing "why wasn't I paid this cycle"
  should check the logs this design writes (`"floors to 0 PV"`, cap-exhaustion) rather than assume
  a payout failure.
- **This whole design was verified end-to-end only after a real, previously-unnoticed gap was
  closed**: CI's actual MySQL-backed test run had been silently skipped for three days by an
  unrelated lint failure, so every test described above was, until that was fixed, only ever proven
  against SQLite locally. See the CI/CD fix commits from this same date for the full story — the
  concurrency claims in this ADR are only as trustworthy as that CI run, which is now green.
