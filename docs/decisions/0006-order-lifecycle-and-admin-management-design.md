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

### 7. PDF invoice is a plain receipt, no GRA withholding-tax logic

Resolved directly from the source doc, Section 5.3's own field list: "Order ID, customer name,
items ordered, delivery method, address, total, and payment status." `WITHHOLDING_TAX_RATE`
(Task 16) applies to distributor wallet *withdrawals*, not customer sales orders — no tax logic
belongs on this invoice. `WeasyPrint` is already a pinned dependency (`requirements.txt`) but has
never actually been used to generate anything yet in this codebase; its real HTML-to-PDF API needs
confirming against real docs at implementation time (`source-driven-development`), not assumed.

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
