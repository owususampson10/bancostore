# ADR-0007: 7-day cooling-off refund design

## Status

Accepted (2026-07-27)

## Date

2026-07-27

## Context

Task 19 (`tasks/todo.md`, opening Phase 7) implements Section 9 of the source doc
(`docs/Bancostore_Features_and_Workflow_v4.docx`): "The 7-Day Cooling-Off Period (Refund Policy)."
Ghana's Direct Selling Act 930 gives every new distributor the right to cancel within 7 days of
joining and get a refund. `tasks/todo.md`'s own acceptance criteria paraphrase this section, but
paraphrase and source diverge in a few places — the same class of gap ADR-0002 (direct referral
bonus formula) and ADR-0006 (order lifecycle) both found and resolved by reading the source
directly rather than trusting the paraphrase, per this project's `source-driven-development`
discipline.

The source doc's Section 9, extracted directly from the `.docx` (not from memory or from
`tasks/todo.md`'s summary):

> Any distributor within their first 7 days of registration [may cancel]. ... How much is refunded?
> The full starter pack price minus a 10% processing fee. Example: Pack B (GHS 2,000) refund =
> GHS 1,800. What happens to PV? All PV added to their upline's legs when they joined is
> automatically removed. What happens to earnings? Any commissions already earned are reversed.
> Any referral bonus paid to their sponsor is also reversed. GHS 100 registration fee? This is not
> refunded. Cooling-off period length: 7 days — set in Admin Settings. Processing fee rate: 10% —
> set in Admin Settings.

Both admin-editable settings already exist in `apps/platform_settings/config.py`'s
`REGISTRATION_AND_MEMBERSHIP_SETTINGS`, seeded since Task 3 but never wired up until now:
`COOLING_OFF_PERIOD_DAYS` (7) and `COOLING_OFF_REFUND_DEDUCTION_RATE` (`Decimal("10")`, a
percentage). `REGISTRATION_FEE_REFUNDABLE` (`False`) already encodes "the registration fee is not
refunded" too. No new constance settings are needed for this task's money math.

Four real design gaps existed between the source doc's wording and what's directly buildable —
put to the user, resolved with real-world MLM/direct-selling industry practice as the grounding
(not guessed, and not decided by the source doc's wording alone, since the doc is silent on all
four):

## Decision

### 1. The 7-day window is anchored to `Distributor.starter_pack_confirmed_at`, not account creation

The source doc says "7 days of registration," but `Distributor.starter_pack_confirmed_at` — not
account creation (`PendingRegistration` consumption, Task 10a/10b) — is the only timestamp that
corresponds to anything actually being refundable: PV, rank, and the sponsor's referral bonus are
all set at starter-pack confirmation (`apps/distributors/services.py::consume_paid_starter_pack`,
Task 10c/10d), not at registration-fee payment. Real-world cooling-off consumer-protection law (the
US FTC Cooling-Off Rule, the EU Consumer Rights Directive, and the Ghana Direct Selling Act 930 this
project is built against) universally attaches the cooling-off right to the *purchase/contract
date*, not an earlier, unrelated account-creation step. Anchoring to account creation instead would
mean a distributor who registers and buys the starter pack 6 days later gets only 1 day to
reconsider the actual purchase — inconsistent with the law's own purpose.

### 2. Binary-tree placement is not removed — the distributor is soft-deactivated, PV/rank/commissions reversed

`apps/binary_tree/services.py::BinaryTree` has no `remove`/`unplace` method anywhere in this
codebase — `place_distributor` is a one-way operation. Building safe `BinaryTreeEdge` closure-table
node removal (which would need to handle re-parenting or vacating for any already-placed downline,
even though a 7-day-old distributor realistically has none) is real, unbuilt, spillover-adjacent
scope the source doc never actually asks for — Section 9 only mentions PV/commission reversal and
"the ability to cancel," never removing the tree node itself.

Real-world MLM platforms diverge here based on maturity: some vacate a cancelled node immediately
(frees the slot for future spillover), others soft-deactivate and leave the slot "dead" to preserve
audit history, building real slot-reclamation later only once it's actually needed at scale. This
project takes the soft-deactivate path: the distributor's tree slot stays in place, but the account
is deactivated the same way `apps/distributors/views.py::distributor_profile`'s existing
suspend/reactivate toggle already works (`user.is_active = False`, already confirmed to genuinely
block login via `apps.distributors.backends.PhoneNumberBackend`) — no new account-state mechanism
needed. **Deferred, not silently skipped:** real tree-slot reclamation, should a real need for it
ever arise once this platform is at meaningful scale.

### 3. "Commissions already earned are reversed" reverses only the sponsor's direct referral bonus

The source doc's wording is broad ("any commissions already earned"); `tasks/todo.md`'s own
acceptance criteria is narrower ("any referral bonus paid to the sponsor"). This task builds the
narrower scope, for two independent reasons:

- A distributor within their first 7 days has essentially never generated Binary/Matching-bonus-
  eligible activity — both bonus types need sustained downline PV activity accumulated over a
  batch cycle or a rolling window (`MATCHING_BONUS_INTERVAL_DAYS`), which a week-old distributor's
  own single starter-pack purchase cannot itself produce for anyone (the direct referral bonus is
  the *only* commission type a single signup can generate synchronously).
- This mirrors ADR-0006's already-accepted "`PvDailyBucket` is a fungible pool shared by every
  ancestor purchase, can't attribute precisely once mixed into an aggregate batch bonus cycle"
  limitation exactly. Real-world direct-selling companies converge on the same practice: reverse
  what's discretely attributable (a one-time referral bonus, a clean single `WalletTransaction`),
  and treat anything already blended into an aggregate batch bonus payout as uneconomical/impossible
  to claw back precisely.

**Deferred, not silently skipped:** if a future scenario is found where a week-old distributor
somehow did generate a Binary/Matching bonus for someone before cancelling, that bonus is not
reversed by this task. Flagged as a known, accepted, currently-theoretical gap — the same
sunk-cost-acceptance shape ADR-0006 already established for order cancellation.

### 4. If the sponsor can't be fully debited, debit what's available and let the distributor's refund proceed

`apps/wallet/services.py::debit()` raises `InsufficientBalanceError` if the sponsor's wallet balance
is lower than the amount being reversed — a real, expected scenario if the sponsor already withdrew
the direct referral bonus before the cancelling distributor exercised their cooling-off right.

Two real-world patterns exist for this: (a) net the shortfall against the sponsor's *future*
earnings as a negative balance/debt ledger entry, or (b) write off the unrecoverable amount as a
cost of doing business. (a) would require negative-balance accounting this codebase has nowhere —
`Wallet.balance` has a hard `CheckConstraint(balance__gte=0)` (Task 15) — real new scope this task
does not take on. This task takes path (b): debit whatever is currently available in the sponsor's
wallet (possibly zero, possibly a partial amount), log the shortfall for manual admin follow-up, and
let the distributor's own refund proceed regardless. Mirrors ADR-0006's identical
already-paid-out-is-an-accepted-limitation precedent from Task 18b exactly — a distributor's
statutory cancellation right must not be blocked by an unrelated wallet state that has nothing to do
with whether their own cancellation request is valid.

### 5. Refund math — confirmed against the source doc's own worked example, not assumed

```
refund_pesewas = starter_pack_price_pesewas * (1 - COOLING_OFF_REFUND_DEDUCTION_RATE / 100)
```

Verified against Section 9's own example: Pack B = GHS 2,000, rate = 10% → refund = GHS 1,800.
`REGISTRATION_FEE` (GHS 100) is never refunded, already encoded by the existing (previously unused)
`REGISTRATION_FEE_REFUNDABLE = False` setting — no new constance setting needed for either figure.

### 6. Architectural reuse: generalize the existing PV-reversal helper instead of duplicating it

`apps/orders/services.py::_reverse_ancestor_pv` (Task 18b) already implements the exact PV-reversal
shape this task needs: reverse `PvLedger` directly (append-only elsewhere, safe to reverse exactly),
and reverse `PvDailyBucket`/`MonthlyPersonalPv` only what's still live (a fungible pool that may
already be partially consumed by a Binary Bonus payout or expired via `PV_CARRY_FORWARD_EXPIRY_DAYS`
— see ADR-0006's own acceptance of this same limitation). It is currently Order-specific (takes a
locked `Order` object and reads `order.pv_earned`/`order.confirmed_at`/`order.customer.distributor`
directly), not reusable as-is for reversing a starter-pack purchase's PV.

This task extracts/generalizes it into `apps/pv_ledger/services.py` as a shared helper taking
`(distributor, pv_amount, purchase_date)` directly, with `apps/orders/services.py` updated to call
the shared version instead of keeping a second, duplicate implementation — a straightforward,
behavior-preserving refactor (Task 18b's own existing test suite is the regression guard for the
order-side caller), not new PV-reversal logic.

### 7. The refund amount is credited to the distributor's own wallet, not paid out externally

The source doc says "GHS 1,800 refunded" but never specifies a payment mechanism. This is a
genuine gap the source doc doesn't resolve, distinct from (and not automatically settled by)
ADR-0006's "no real Paystack Refund API, manual admin follow-up" precedent for *order* refunds —
that precedent exists because a storefront customer's payment method (their card) has no
in-system representation to credit back to. A distributor, unlike a customer, already has a real
`Wallet` (`apps/wallet`) with an established `credit()`/`debit()` ledger and an existing withdrawal
flow (Task 16) built specifically to let them cash out. Crediting the refund there keeps the whole
flow in-system and auditable, and gives the distributor a genuine (if currently Paystack-account-
tier-limited, per the pre-existing `project_paystack_transfer_account_tier_blocked` constraint) path
to actually receive the money, rather than depending on an admin process this system has no
visibility into.

### 8. Two new `WalletTransaction.TransactionType` values, not reused existing ones

This task moves money in two directions that need their own transaction types, not a reuse of
`REFUND_REVERSAL` (already explicitly documented on the model as "for product-order refunds, a
different domain entirely," per a Task 16f `doubt-driven-development` finding that reusing a type
across unrelated reasons money re-enters a wallet is itself a bug class worth naming and avoiding)
or `WITHDRAWAL_REVERSAL` (a failed-Transfer-specific type, also unrelated):

- `COOLING_OFF_REFUND` — credited to the *cancelling distributor's own* wallet (Decision 7).
- `DIRECT_REFERRAL_BONUS_REVERSAL` — debited from the *sponsor's* wallet (Decision 4), named
  symmetrically with the existing `WITHDRAWAL_DEBIT`/`WITHDRAWAL_REVERSAL` pairing convention
  already established on this same model.

No schema/constraint change is needed beyond the new `choices` entries themselves (confirmed no
`CheckConstraint` anywhere on this model enumerates `transaction_type`'s exact values) — a migration
is still required for Django's own field-state tracking, per this model's own established precedent
(Task 16f's `WITHDRAWAL_REVERSAL` addition needed one too).

## Alternatives Considered

### Anchor the cooling-off window to account/registration creation

Matches the source doc's literal "7 days of registration" wording. Rejected: doesn't match how the
underlying consumer-protection law it's modeled on actually works (cooling-off rights attach to
purchase/contract dates), and produces a genuinely worse outcome for a distributor who registers
before buying a starter pack.

### Remove the distributor's `BinaryTreeEdge` placement on cancellation

Matches "the distributor un-joins entirely" intuition. Rejected for this task: no removal mechanism
exists anywhere in this codebase, building one safely is large unbuilt scope the source doc doesn't
ask for, and a soft-deactivated week-old node with reversed PV/rank/commissions is functionally
inert for every purpose (further bonus cycles, dashboard display, etc.) without needing structural
tree surgery.

### Reverse any Binary/Matching bonus the cancelling distributor personally earned too

Matches the source doc's broader wording literally. Rejected for this task's initial scope: adds
real implementation complexity (would need per-distributor bonus-history attribution this codebase
doesn't build anywhere) for a scenario that's realistically never triggered within a 7-day window,
per the reasoning in Decision 3.

### Block the cooling-off refund entirely if the sponsor's wallet can't be fully debited

Simpler code (no partial-debit/shortfall-logging path needed). Rejected: would let an unrelated
third party's wallet state (the sponsor already spent money that was legitimately theirs at the
time) block a distributor's own statutory right to cancel and be refunded — the wrong party bears
the consequence.

### No automated refund payout — manual admin follow-up only, mirroring ADR-0006 exactly

Matches this project's own most recent precedent for money leaving the system. Rejected: unlike an
order-refund customer, a distributor already has a `Wallet` this codebase built specifically to
hold their money and an existing withdrawal flow to cash it out — routing the refund outside that
system when the correct in-system destination already exists would be inconsistent, not simpler.

## Consequences

- `Distributor.starter_pack_confirmed_at` becomes load-bearing for a second purpose (previously
  only idempotency for `consume_paid_starter_pack`) — no schema change needed, the field already
  exists.
- The soft-deactivation path means a cancelled distributor's tree slot remains permanently occupied
  under current scope — a real, accepted trade-off (wasted tree capacity in exchange for not
  building unrequested closure-table surgery), not a bug.
- `apps/pv_ledger/services.py` gains a new public PV-reversal helper; `apps/orders/services.py`'s
  own cancellation/refund path (Task 18b) is refactored to call it instead of its private
  `_reverse_ancestor_pv` — verified behavior-preserving by Task 18b's own existing test suite
  passing unchanged after the refactor, not a new test-writing exercise for that side.
- The sponsor's direct referral bonus reversal can leave a real, permanent, unrecovered shortfall if
  the sponsor already withdrew it — an accepted business risk, logged for admin visibility, not
  silently absorbed.
- `apps/wallet/models.py::WalletTransaction.TransactionType` gains `COOLING_OFF_REFUND` and
  `DIRECT_REFERRAL_BONUS_REVERSAL`, needing a migration for Django's own field-state tracking (no
  data migration, no constraint change).
