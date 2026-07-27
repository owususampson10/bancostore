# ADR-0006: Order status lifecycle, cancellation/refund reversal, and admin order management design

## Status

Accepted (2026-07-26)

## Date

2026-07-26

## Context

Task 18 (`tasks/todo.md`) covers the order status lifecycle (Pending → Confirmed → Processing →
Dispatched → Delivered / Cancelled / Refunded), notifications on each transition, an admin order
management view (filters, PDF invoice, cancel/refund actions), and an auto-cancel-unpaid-orders
Celery task. Unlike Task 17, no ADR existed yet resolving its real design gaps — this one does
that before any code exists, per this project's own established practice (ADR-0004 for Task 16,
ADR-0005 for Task 17).

The primary source doc (`docs/Bancostore_Features_and_Workflow_v4.docx`, Section 5.2–5.3) was read
directly — extracted to plain text via macOS `textutil` since it's a `.docx` — rather than relied
on from memory or from `SPEC.md`'s own summary of it, per this project's established
`source-driven-development` discipline (the same doc's own worked examples already corrected this
project's Direct Referral Bonus and Matching Bonus formulas earlier, and its own Section 5.1
numbers already grounded ADR-0005's delivery-zone fees). Several of the questions raised going
into this task turned out to already have an answer in that section; the rest were put to the
user directly.

## Decision

### 1. Transition ownership — resolved directly from the source doc, Section 5.2's own table

| Status | Who updates it | Already built? |
|---|---|---|
| Pending | System (automatic) | Yes — Task 17c, `create_pending_order` |
| Confirmed | System (automatic) | Yes — Task 17d, `confirm_order_payment` |
| Processing | Admin | No — Task 18 |
| Dispatched | Admin | No — Task 18 |
| Delivered | "Admin or Delivery" per the source doc | No — Task 18 |
| Cancelled | "Customer or Admin" per the source doc | No — Task 18 (admin only; see decision 3) |
| Refunded | Admin | No — Task 18 |

**Stated assumption, not silently decided:** the source doc's "Delivery" role for the Delivered
transition has no corresponding user type anywhere in this codebase (`SPEC.md`'s three roles are
customer/distributor/admin only, and no "Delivery" role has been proposed or built at any point in
this project). Delivered is Admin-only for Task 18, matching every other manual transition. Revisit
only if a real delivery-partner-facing feature is ever separately scoped.

### 2. Cancelling/refunding a CONFIRMED order reverses both stock and PV — user-confirmed 2026-07-26

An order can be cancelled "before dispatch" per the source doc's own wording — meaning a `CONFIRMED`
order (payment verified, stock already decremented, PV already credited by
`apps/orders/services.py::confirm_order_payment`, Task 17d) can still be cancelled. Leaving that
stock/PV credit in place was the original draft's instinct (framed as the "simple, safe" option),
but walked through with the user, it isn't safe — it's a real, quantifiable financial-correctness
gap, not a cosmetic one:

- **Binary Bonus overpayment.** Per this project's carry-forward rule (`apps/pv_ledger/`,
  Task 13), uncredited leg PV rolls forward for up to 180 days waiting to be matched. If the
  cancellation happens before a bonus cycle consumes that PV, leaving it in place means a *future*
  cycle pays a real-money binary bonus to an ancestor, calculated in part on a sale that no longer
  exists.
- **False monthly eligibility.** `MonthlyPersonalPv` gates whether the purchasing distributor stays
  bonus-eligible that month (`config.MIN_MONTHLY_PERSONAL_PV`). Uncorrected, a distributor could
  keep earning commissions for a month they shouldn't have qualified for.

**Decision: reverse both.** Stock reversal is straightforward (give the decremented units back,
mirroring `apps.catalog.services.decrement_stock`'s own locking shape in reverse). PV reversal is
mechanically similar to crediting — but see the real implementation subtlety in the Consequences
section below, found while grounding this decision, not glossed over.

**Explicitly accepted limitation, not built around:** if a binary/matching bonus cycle already paid
out on that PV *before* the cancellation happens, reversing the ledger now does not claw back money
that already left the company's wallet system to the ancestor. No wallet-clawback mechanism is
built for this — it would be a much larger, separate feature (debiting a distributor's wallet for a
bonus they already received, in good faith, based on data that was correct at the time). Flagged
here as a known, accepted sunk-cost scenario for this specific edge case, not silently ignored.

### 3. Refunded stays manual — no real Paystack Refund API integration in Task 18

The source doc says "Refunded: Payment was refunded to the customer," which could be read as
requiring real payment-provider integration. Confirmed with the user: **manual only**, matching
Task 17d's stock-insufficiency precedent exactly (`_cancel_order_for_insufficient_stock`) — admin
marks the order `Refunded` after processing the actual refund outside the system. Building real
Paystack Refund API integration would be new payment-integration surface requiring its own
`security-and-hardening` pass and explicit sign-off per `SPEC.md`'s Boundary on Paystack
integration code — a distinct decision from the rest of this task, not bundled in here. Revisit as
its own scoped task if/when real refund automation is wanted.

### 4. Customer-facing self-service cancel deferred to a follow-up task

The source doc's own table lists Cancelled as triggerable by "Customer or Admin," but
`tasks/todo.md`'s existing Task 18 scope only ever mentions admin updating status. Confirmed with
the user: **defer.** A customer-facing cancel action needs its own eligibility rules (cancellable
only before Processing? Only within some time window?) and UI/UX — real, separate scope, not
folded into an already multi-part task. Task 18 ships admin-side cancel only; customer self-service
cancel is explicitly out of scope here, tracked as a future task, not silently dropped.

### 5. Admin order management is a custom Stitch-designed `admin_portal` page

Matches the established pattern for every prior admin screen (KYC Review Queue, Withdrawal Review
Queue, Distributor Directory, Commission Cycle Detail, Task 22/23) rather than plain Django Admin's
changelist. Confirmed with the user 2026-07-26. Needs a fresh Stitch prompt before the frontend
slice starts, same as every prior admin page.

### 6. Auto-cancel only ever applies to unpaid (`pending`) orders — no reversal needed there

Resolved directly from the source doc: "Unpaid orders are automatically cancelled after a time
period set in admin settings." Nothing has been charged, decremented, or credited for a `pending`
order (per ADR-0005 decision 3/4) — auto-cancel is a pure status transition plus a notification, no
stock/PV interaction at all. Decision 2 above only applies to cancelling an already-`confirmed`
order, a materially different and rarer path (admin-initiated, not automatic).

**Implementation confirmed 2026-07-26 (Task 18d):** the batch driver's own audit trail is a new,
dedicated `OrderCycleRun`/`OrderCycleFailure` model pair, not a third `job_name` value on
`CommissionCycleRun`/`Failure` — that model requires a `total_amount` field this money-free job has
nothing to fill, and the same reasoning `apps/withdrawal/models.py::WithdrawalCycleRun` already
gives for staying its own model applies here too, only more so (Withdrawal at least deals in money;
this job deals in none). The batch driver itself is hand-written, mirroring
`process_withdrawal_payouts`'s shape rather than calling into `apps.commissions.tasks
._run_commission_cycle`, whose `process_one` contract requires a `Decimal` return.

### 7. PDF invoice is a plain receipt, no GRA withholding-tax logic

Resolved directly from the source doc, Section 5.3's own field list: "Order ID, customer name,
items ordered, delivery method, address, total, and payment status." `WITHHOLDING_TAX_RATE`
(Task 16) applies to distributor wallet *withdrawals*, not customer sales orders — no tax logic
belongs on this invoice. `WeasyPrint` is already a pinned dependency (`requirements.txt`) but has
never actually been used to generate anything yet in this codebase; its real HTML-to-PDF API needs
confirming against real docs at implementation time (`source-driven-development`), not assumed.

**Implementation note (Task 18e, 2026-07-26): WeasyPrint cannot run on this project's local dev
Mac.** `HTML(string=...).write_pdf()` is confirmed correct against WeasyPrint's own docs, but its
`__init__` eagerly `dlopen()`s the system Pango library at import time, and this Mac (macOS 12) is
an unsupported Homebrew Tier-3 configuration — `brew install weasyprint` failed after ~50 minutes
(it had to compile Python 3.13 from source along the way) with `cffi` unable to build. Verified for
real in CI instead: `.github/workflows/ci.yml`'s `test` job now installs `libpango-1.0-0`/
`libpangocairo-1.0-0` via `apt` (an ordinary pre-built Ubuntu package, no compiling needed there).
The real end-to-end PDF test uses this codebase's first `pytest.mark.skipif`, conditioned on
whether `import weasyprint` actually succeeds — self-healing, not a permanent skip.

## Alternatives Considered

### Leave PV/stock uncorrected on cancellation of a confirmed order

Covered under decision 2 above — rejected once the actual downstream harm (future binary bonus
overpayment, false monthly eligibility) was worked through with the user; not actually the "safe"
option it first appeared to be.

### Build real Paystack Refund API integration as part of Task 18

Covered under decision 3 above — rejected as new payment-integration scope requiring its own
sign-off, bundled separately rather than folded into this task.

### Build customer-facing cancel now, matching the source doc's literal wording

Covered under decision 4 above — rejected as real, separate scope needing its own eligibility
design, deferred rather than bundled.

## Consequences

- **A new `apps/orders/services.py` function (or small set of functions) is needed for the
  cancel/refund transition**, mirroring `confirm_order_payment`'s locked, idempotent shape:
  lock the `Order` row, validate the transition is legal from the current status, and — only when
  reversing a `confirmed` order — reverse stock and PV inside that same lock, transition status,
  notify after the lock releases.
- **A real implementation subtlety, found while grounding decision 2, not solved here:**
  `apps/pv_ledger/services.py::record_purchase_pv` internally hardcodes `today = timezone.now().date()`
  when crediting `PvDailyBucket` (the structure Binary Bonus's 180-day carry-forward reads), and
  `record_personal_pv` hardcodes `period = timezone.now().date().replace(day=1)` when crediting
  `MonthlyPersonalPv`. Neither function has any way to target the *original* purchase's date/month.
  A naive `record_purchase_pv(distributor, -pv_amount)` call at cancellation time — which could be
  days or weeks after the original `confirmed_at` — would incorrectly adjust *today's* daily bucket
  and *this* calendar month's personal PV, not the ones the original credit actually landed in.
  `PvLedger.left_leg_pv`/`right_leg_pv` (the running totals Binary Bonus's own calculation reads,
  per Task 13's "only reads pre-aggregated counters" design) have no such date dependency and are
  safe to reverse directly. This needs its own `doubt-driven-development` pass at implementation
  time to design correctly — likely either extending `record_purchase_pv`/`record_personal_pv`
  with an explicit historical-period parameter, or writing dedicated reversal functions that touch
  only what's safe and meaningful to reverse (the leg-PV running totals, and the *correct* month's
  `MonthlyPersonalPv` row) while leaving `PvDailyBucket` alone — consistent with decision 2's own
  "already-paid-out bonus is a sunk cost" acceptance, since a daily bucket already consumed by a
  completed cycle has nothing meaningful left to reverse anyway.
- **A new `Order.tracking_note` field is likely needed** ("Admin can update the order status and
  add a tracking note," Section 5.3) — a schema change requiring explicit sign-off per `SPEC.md`'s
  Boundary, tracked as its own ask before any migration, same as every prior schema slice in this
  project.
- **The admin order management page needs a Stitch prompt from the user before its frontend slice
  starts**, per decision 5.
- **No new Paystack integration surface, no wallet-clawback mechanism, no customer-facing cancel
  UI** are in scope for Task 18, per decisions 2–4 — each flagged explicitly as deferred rather than
  silently out of scope.

## Update (2026-07-26): Task 18b design finalized via a three-cycle `doubt-driven-development` pass

Three rounds of fresh-context adversarial review (each grounded in the real code, not assumption)
resolved the subtlety flagged above and found several more. Corrections and final decisions:

- **Correction: the claim above that "Binary Bonus's own calculation reads" `PvLedger` was wrong.**
  `apps/commissions/services.py::process_binary_bonus_for_distributor`'s own docstring states
  plainly: "Never touches PvLedger... reads/writes only PvDailyBucket." `PvLedger` is actually read
  by exactly two things: `apps/binary_tree/services.py::BinaryTree._weaker_leg` (the auto-balance
  fallback's weak-leg comparison when placing a **new** distributor with no explicit leg choice),
  and a read-only ancestor-aggregate admin/reporting display. This changes the stakes of decrementing
  it: not a Binary-Bonus-overpayment risk (that risk lives entirely in `PvDailyBucket`), but a
  tree-placement-fairness question instead.
- **Decision: reverse `PvLedger` too, not just `PvDailyBucket`/`MonthlyPersonalPv`.** Confirmed with
  the user against real-world MLM practice: compensation-plan "returns policies" universally reverse
  the underlying volume on a refund, not just the commission — leaving it un-reversed opens a real
  gaming vector (buy-then-refund under a specific leg to permanently bias that leg's apparent
  strength, steering future unspecified-leg registrants away from it for free). `PvLedger` is a
  single running counter here, not a separate immutable transaction ledger, so there's no compliance
  reason to preserve monotonicity. **The two docstrings currently calling it "immutable all-time
  historical total"** (`process_binary_bonus_for_distributor` and
  `apps.pv_ledger.services.sum_leg_pv`) **must be corrected**, not left describing a no-longer-true
  contract.
- **The date-drift subtlety flagged above is fixed without a new migration or field.**
  `confirm_order_payment` captures one `now = timezone.now()` at the point PV crediting begins,
  passes `now.date()` explicitly into both `record_purchase_pv` and `record_personal_pv` (each gains
  a new optional `today=` parameter, backward-compatible with their only other caller,
  `apps.distributors.services.consume_paid_starter_pack`), and reuses that same `now` for
  `order.confirmed_at` — guaranteeing `order.confirmed_at.date()` is always exactly the date the
  original PV credit landed on, by construction, not by re-derivation from two independent
  `timezone.now()` calls with real retry-and-sleep work happening between them.
- **New, deeper limitation found and accepted (broader than the sunk-cost acceptance in decision 2
  above): `PvDailyBucket` is a fungible pool, not a per-order ledger.** `_credit_daily_buckets`
  already merges every same-day order for the same ancestor+leg into one row, and
  `consume_leg_pv_fifo` drains it without recording whose contribution it spent. A reversal that
  only checks "is the pool currently ≥ this order's amount" cannot distinguish reversing *this
  order's own* still-there PV from incorrectly clawing back a *different, still-valid* sibling
  order's PV that happens to add up to enough. Building real per-order PV attribution would mean
  turning `PvDailyBucket` from an aggregate into a per-order ledger — a much larger schema change
  than Task 18b's scope. **Accepted as a documented limitation**, same spirit as decision 2's
  already-paid-bonus acceptance, just wider than originally scoped there.
- **Decision: refund restocking is an explicit admin choice, cancellation is not.** Matches standard
  e-commerce practice (e.g. Shopify's refund flow has an explicit "Restock items" checkbox, off by
  default) rather than assuming refunded always means physically returned — a goodwill refund on an
  already-delivered order, or a damaged-item refund the customer keeps, must not silently inflate
  `Product.stock` for units never actually returned. Pre-dispatch cancellation has no such ambiguity
  (goods never shipped) and always restocks automatically. The new
  `apps/orders/services.py::cancel_or_refund_order` function takes `to_status` restricted to only
  `CANCELLED`/`REFUNDED` (a caller-bug guard the draft design was initially missing — a permissive
  version could have been miscalled with a normal lifecycle transition like `PROCESSING` and
  silently reversed a healthy order's PV/stock) and a `restock` parameter required (no silent
  default) whenever `to_status == REFUNDED`.
- Two smaller implementation-discipline fixes carried into Task 18b's actual code: the notification
  helper this function calls after its lock releases must wrap each channel in its own
  `try/except Exception`, matching `_send_confirmation_notifications`'s existing convention exactly
  (an uncaught exception there would otherwise risk `retry_on_lock_contention` re-running an
  already-committed transaction a second time); and a `PvLedger` reversal shortfall is logged at
  `ERROR`, not `WARNING`, unlike `PvDailyBucket`/`MonthlyPersonalPv` — since nothing else has ever
  decremented `PvLedger`, a shortfall there signals a real accounting bug, not an expected gap.
