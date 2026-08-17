# ADR-0011: Deploy downtime mitigation — expand/contract migrations now, dual-Daphne blue/green deferred

## Status

Accepted (2026-08-17)

## Date

2026-08-17

## Context

`deploy/README.md`'s documented deploy runbook stops all three Supervisor programs
(`bancostore-daphne`, `bancostore-celery-worker`, `bancostore-celery-beat`) before running
`migrate`/`collectstatic`, then starts them back up — a real, if brief ("typically well under a
minute"), downtime window on every deploy that touches migrations. This was an accepted tradeoff
when Task 24 shipped, explicitly noting "the alternative (a zero-downtime blue/green setup) is
real infrastructure this project doesn't have yet." At the time, that was reasonable: zero real
users.

The concern raised directly by the user: once Bancostore has real distributors and customers
actively transacting, that window stops being free. A Paystack callback or webhook landing during
the restart, or a customer mid-checkout, would see the app briefly unreachable.

True blue/green (two separate servers behind a load balancer, one taking traffic while the other
deploys) isn't available here — this project runs on a single Hostinger VPS (`deploy/README.md`
Server facts). Two mitigations were discussed as the single-VPS-compatible version of the same
idea:

1. **Expand/contract migrations** as a standing practice.
2. **Dual-Daphne behind Nginx** — two app processes on the same VPS on different ports, with
   Nginx routed to whichever one is currently healthy, so a restart of one never drops traffic.

## Decision

Adopt (1) now, defer (2) explicitly until real traffic makes it worth the added complexity.

### Why (1) is worth adopting now, even though today's deploy model doesn't strictly need it

It's worth being precise about *why*, since the current runbook's stop-everything-then-migrate-
then-start-everything shape means there is never a moment where old application code runs against
new schema, or new code runs against old schema — everything is down together, so a plain
(non-expand/contract) migration can't corrupt data under today's deploy model specifically. The
value of adopting expand/contract now is real but different from "prevents corruption during
today's deploys":

- **It's the prerequisite for (2) working safely at all.** The moment two Daphne instances run
  concurrently against one shared MySQL database during a rolling cutover, a destructive migration
  (drop/rename a column, add `NOT NULL` with no default, a type change) can crash or corrupt data
  in whichever instance hasn't cut over yet. Adopting expand/contract as the house style now means
  (2), whenever it's built, has nothing left to retrofit — every migration written between now and
  then is already rolling-deploy-safe.
- **It makes rollback safer even under today's stop-the-world model.** If a bad deploy needs to be
  rolled back to the previous commit, expand-phase-only migrations mean the previous code version
  still works against the new schema (it just ignores the new column/table), instead of crashing
  because a column it expects is already gone.
- **It matches conventions this codebase already uses for the same reason.** `Order`/`OrderItem`'s
  price/PV/delivery-fee snapshot fields, `OrderItem.stock_decremented` (Task 44), and
  `Order.backorders_allowed_at_checkout` all exist so that a later change to live data or settings
  never retroactively breaks something already committed — expand/contract is the same
  backward-compatibility instinct applied to schema changes specifically.

### The concrete practice, starting with the next migration that touches an existing column/table

- **Expand deploy:** add the new column/table (nullable, or with a server default so existing rows
  are valid immediately); if data is moving, write to both the old and new locations from this
  point on; old code paths keep working completely unmodified.
- **Cut-over deploy** (can be the same deploy as expand, for a small change; a separate one for a
  larger data migration): switch application code to read from the new column/table.
- **Contract deploy:** once confident nothing depends on the old shape anymore, drop the old
  column/table in its own migration.
- Never combine "add the destructive change" and "code that only works with the new shape" in one
  migration when the change touches a column/table live code already reads or writes.
- This is a **practice for future migrations**, not a retrofit of existing ones — no migration in
  the current schema needs to be rewritten because of this decision.

### Why the risk today is smaller than it first looks (not a reason to skip (1), just context)

This codebase's Paystack integration is already built idempotent end-to-end — `confirm_order_
payment`, `consume_paid_starter_pack`, and the shared `paystack_webhook` dispatcher all
re-verify server-side and are safe to run twice, specifically because Paystack itself retries
webhook delivery on failure. A webhook or callback landing during a deploy's brief downtime window
gets a connection failure, Paystack retries per its own delivery schedule, and the retry succeeds
once the app is back — the same protection that already covers an ordinary transient network blip
today. This doesn't eliminate the concern (a customer's browser request during the window still
sees a failure, not a silent retry), but it means the failure mode is "briefly unavailable," not
"lost or double-processed payment."

## Deferred: dual-Daphne behind Nginx

**Trigger condition, written down so it isn't silently forgotten:** build this once real
production traffic makes a short restart window actually matter — i.e., once the platform has
real distributors/customers transacting regularly, not the current state (see the 2026-08-17
production check: 1 total user, 0 orders, ever). Revisit this ADR when that changes.

Sketch of the approach, for whoever picks this up: run two `daphne` processes on the same VPS on
different ports (e.g. `8001`/`8002`), both pointed at the same codebase checkout; Nginx proxies to
whichever is currently marked healthy. A deploy takes the inactive one out of rotation, updates
and restarts it, waits for a health check to pass, switches Nginx to it, then repeats for the
other — real users never hit a stopped process. This needs: an Nginx config capable of switching
upstreams (or `nginx -s reload` after editing the proxied port), a health-check endpoint, and a
deploy script update — real work, correctly not built speculatively ahead of the traffic that
would justify it. Celery worker/beat are unaffected by this pattern (they don't serve live
requests; `bancostore-celery-beat` in particular must never run as two concurrent instances, so
dual-Daphne must not be extended to Celery beat without separate design work).

## Alternatives Considered

### Build dual-Daphne now, ahead of real traffic

- Pros: fully solves the downtime problem immediately.
- Rejected: real infrastructure and ongoing maintenance cost (two processes to monitor, a
  health-check mechanism, more complex deploy tooling) for a platform currently at zero real
  transacting users. Matches this project's own repeated pattern of not building speculative
  scale infrastructure ahead of the traffic that justifies it.

### Do nothing until traffic arrives, revisit both mitigations then

- Pros: zero cost today.
- Rejected for the expand/contract half specifically: it costs nothing to adopt as a writing
  practice starting now, and doing so avoids a future scramble to retrofit backward-compatible
  migration discipline right when it's suddenly urgent (real traffic already present).

## Consequences

- Every migration written from this point forward that touches an existing column/table on a
  live table needs to be split into expand/contract steps — a small ongoing discipline cost,
  not a one-time cost.
- Dual-Daphne remains a real, tracked gap until built — see `tasks/todo.md`'s Known Issues section
  for the tracked entry and its trigger condition.
- No code changes ship from this ADR alone; it documents a decision and a deferred item, matching
  how ADR-0005's deferred real-Paystack-refund-automation note and similar entries in this project
  work.
