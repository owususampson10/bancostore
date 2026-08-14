# ADR-0010: Sales & Revenue Reporting — query architecture at scale

## Status

Accepted (2026-08-14)

## Date

2026-08-14

## Context

Task 46 (source doc Section 12.4) needs revenue by day/week/month, orders-per-status, delivery
fees collected by zone, best-selling products, new-vs-returning customers, and commissions-paid-
vs-revenue-earned, each filterable by an admin-chosen date range and exportable via Task 45's
`bancostore/exports.py`. `SPEC.md`'s Scale Architecture section targets hundreds of thousands of
users; the codebase already has one concrete precedent for the failure mode this task must avoid:
Task 27's Admin Dashboard (`apps/admin_portal/views.py::dashboard`) runs ~10 separate live
aggregate queries on every page load, most of them full-table `.count()`/`.aggregate()` scans with
no date bound. That's accepted there because the dashboard shows fixed, cheap windows ("this
week") and is viewed by one admin occasionally. A reporting screen is structurally different: an
admin picks an **arbitrary date range** (a specific day, a custom month, "since launch"), so the
query shape can't be a single hardcoded window — and unlike the dashboard, the whole point of this
task is that these numbers get looked at *and exported*, which invites exactly the kind of "run it
over a year of history" query the dashboard never has to answer.

Three approaches were on the table (framed in `tasks/todo.md`'s own 46a description):

1. **Pre-aggregated daily rollup tables**, matching what the todo.md description calls "the PV
   ledger's own event-driven-aggregate precedent."
2. **A scheduled Celery report-cache job.**
3. **A bounded live-query lookback window.**

Research before writing this ADR found the PV ledger comparison in (1) is not quite apt, and that
matters for the decision:

- `PvLedger`/`PvDailyBucket` (`apps/pv_ledger/models.py`, `apps/pv_ledger/services.py`) are
  genuinely **event-driven**: `record_purchase_pv` writes at order-confirmation time, synchronously,
  inside the same locked transaction as the purchase itself. That's necessary there because Binary
  Bonus's carry-forward math needs to see PV as of *right now*, not as of last night — a distributor
  buying something and immediately checking their dashboard expects their PV to already be reflected.
- Task 46's numbers have no equivalent freshness requirement. Nobody's payout depends on a sales
  report being current to the second; a report that's accurate as of the last scheduled refresh is
  fine, matching how every other batch-style feature in this codebase already treats staleness
  (Binary/Matching Bonus run every `BINARY_BONUS_INTERVAL_MINUTES`, not on every PV credit).
- Building true event-driven rollups for reporting would mean adding a write hook to every place
  revenue/order-status/delivery-fee/commission data changes — order confirmation, `advance_order_
  status`, `cancel_or_refund_order`, three separate commission-credit call sites, withdrawal
  payout — five-plus independent write paths, each a place a future change could silently drift
  from the rollup without anyone noticing. That's a real, ongoing maintenance cost this task's
  actual freshness requirement doesn't justify paying.
- Neither `Order.created_at` nor `WalletTransaction.created_at`/`transaction_type` currently has a
  dedicated index (`Order.status` does — `apps/orders/models.py`, cited explicitly for "hundreds of
  thousands of users" scale reasoning; `created_at` does not). `order_management_queue`'s existing
  date-range filter (`apps/admin_portal/views.py::_filtered_orders`) already scans this unindexed
  column live today — a real, pre-existing gap this task's own research surfaced, not invented for
  this ADR.

## Decision

A **hybrid**, split by what each report actually needs — not one blanket rule for all six reports:

### 1. Index `Order.created_at`

Regardless of which option below wins for any individual report, `Order.created_at` gets a
`db_index=True` (a plain migration, no data change). This closes the gap `order_management_queue`
already has live today and is a prerequisite for every option considered below.

### 2. Revenue, orders-per-status, delivery-fee-by-zone, and best-selling-products: a Celery Beat
daily rollup job

A new `apps/reporting` app owns two small tables, both `(date, ...)`-keyed:

- `DailyOrderRollup` — one row per calendar day: revenue, order counts per status, delivery fees
  collected per zone (Kumasi/Accra/Other Regions).
- `DailyProductSales` — one row per `(date, product)`: units sold, revenue.

A Celery Beat job (46b's own sub-scope) computes and upserts *yesterday's* row(s) once per day,
reusing this codebase's already-proven scheduled-job shape wholesale rather than inventing a new
one: `django_celery_beat` `PeriodicTask`/`IntervalSchedule` seeded by migration, self-syncing its
interval from a live constance setting the same way `_sync_periodic_task_interval`
(`apps/commissions/tasks.py`) already does, a per-iteration-renewed Redis lock against overlapping
runs, and a `CommissionCycleRun`/`Failure`-style audit-trail pair (a new `ReportRollupRun`/
`Failure`, mirroring the shape, not literally reusing the commissions-specific model) so a failed
rollup is visible, not silent.

Reports then query these small rollup tables, never the raw `Order`/`OrderItem` tables directly —
summing N *daily* rows (a few thousand even after years of operation) instead of scanning
potentially hundreds of thousands of order rows, regardless of how wide a date range an admin
picks. This is what actually delivers "any date range, still fast," which a live query alone
(even indexed) can't guarantee once order volume is large enough.

### 3. New-vs-returning customers and commissions-paid-vs-revenue: a live, indexed, bounded query

These two don't reduce cleanly to a daily sum:

- **New-vs-returning** needs to know, per order in the requested range, whether that's the
  customer's first order *ever* — a fact that depends on each customer's full history, not
  something a per-day rollup can precompute without effectively becoming a second customer-keyed
  rollup table (real complexity a two-report feature doesn't currently justify).
- **Commissions-paid-vs-revenue** joins `WalletTransaction` (commission types) against
  `DailyOrderRollup`'s already-computed revenue for the same range — the commissions side is a
  live, indexed, date-bounded `WalletTransaction` query; the revenue side reuses (2)'s rollup.

Both run as live queries over `Order.created_at`/`WalletTransaction.created_at`, now indexed by
(1), **bounded to the admin's selected date range** (never "no range = full table") — mirroring
`order_management_queue`'s own existing bounded-date-range convention
(`_filtered_orders`), not inventing a new query shape.

## Alternatives Considered

### Pure event-driven rollups on every relevant write path (the literal PV-ledger pattern)

- Pros: Reports would be current to the second, matching PV ledger's own freshness.
- Rejected: Task 46's numbers have no freshness requirement that justifies this — nothing downstream
  depends on report data being live-to-the-second. Five-plus independent write sites (order
  confirmation, three status-transition functions, commission credit) would each need a rollup
  write hook, a real ongoing drift risk for a feature this codebase's own Binary/Matching Bonus
  precedent already treats as acceptable to run on a periodic cycle instead.

### Pure bounded live-query for every report, no rollup tables at all

- Pros: Simplest to build — no new models, no new Celery job, no audit-trail plumbing. Always
  perfectly fresh.
- Rejected: even with `Order.created_at` indexed, a report covering a wide range (a full year, "all
  time") at this project's target scale still means scanning and aggregating a potentially large
  number of raw order rows on every report view *and* every export — the exact anti-pattern
  `SPEC.md`'s Scale Architecture section already ruled out once for the binary tree/commission
  engine ("do not implement the naive... walk... every cycle" — the same reasoning applies to
  reporting over raw transactional tables instead of pre-aggregated ones).

### One single rollup table covering everything, including new-vs-returning and commissions-vs-revenue

- Pros: One job, one query shape, maximum consistency.
- Rejected: new-vs-returning customers isn't a per-day aggregate at all — it's a per-customer fact
  (is this order this customer's first) evaluated per row in the requested range, which a daily
  rollup can't represent without itself becoming a customer-keyed table, real added complexity
  with no current requirement (a second report, cross-checking a different dimension) driving it.
  Splitting by what each report structurally needs is simpler than forcing one shape to fit all six.

## Consequences

- **Rollup-backed reports (revenue/status/zone/products) are only as fresh as the last completed
  daily job run** — an admin viewing "today so far" before the job has run for today sees no data
  for today yet. Accepted: this mirrors every other Celery Beat batch feature in this codebase
  (Binary Bonus, auto-cancel-unpaid-orders) already showing a similar “as of last cycle” framing,
  and Task 46b's own UI must say so explicitly (e.g. "as of &lt;last rollup run time&gt;"), not
  imply live data.
- **A rollup miscomputation or missed day requires a backfill mechanism** — 46b must include a way
  to (re)compute a specific past day's rollup row on demand (an idempotent upsert, not an
  append-only insert), for the case where the job fails one night and needs to catch up. This is a
  real requirement this ADR surfaces for 46b's own acceptance criteria, not something to discover
  mid-implementation.
- **Two different query shapes exist side by side in the same feature** (rollup-table reads for
  four reports, live-indexed-bounded reads for two) — a future contributor extending this app must
  pick the right shape per new report based on the same test this ADR applies (does it reduce to a
  per-day/per-entity aggregate, or does it need per-row history), not assume one pattern fits
  everything by default.
- **`Order.created_at` gaining an index is a real, if small, migration** — per this codebase's
  Boundaries ("any database schema/migration change... requires asking first"), this is called out
  explicitly here rather than folded silently into 46b's implementation commit.
