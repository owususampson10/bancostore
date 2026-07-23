# ADR-0004: Withdrawal payout design — recipient capture, debit timing, and the Friday batch

## Status

Accepted (2026-07-22)

## Date

2026-07-22

## Context

Task 16 (Withdrawal request flow) has no prior design to draw on — Tasks 12-15 built the earning
side of the wallet (`credit()`, three commission types), but nothing has ever called `debit()`
(`apps/wallet/services.py`) or moved money *out* of the platform. `SPEC.md` gives the business
rule shape (KYC-gated, minimum/maximum amount, once-weekly, `WITHHOLDING_TAX_RATE` deducted, admin
approves, Paystack pays out) but is silent on four implementation questions that each have a real
money-safety consequence if guessed wrong. `apps/distributors/paystack.py` today only wraps
Paystack's Transaction (charge) API — nothing exists yet for Transfer (payout). `Distributor` has
no field of any kind for where a payout should go, despite distributors registering by phone.
`AUTO_APPROVE_WITHDRAWALS_ENABLED`/`AUTO_APPROVE_WITHDRAWAL_THRESHOLD` were seeded generically back
in Task 3 alongside 71 other constance settings, before any task-specific design existed, and sit
in direct tension with `SPEC.md`'s Boundary: "Never auto-approve withdrawals... even temporarily."

All four questions below were put to the user directly rather than assumed, per `SPEC.md`'s
Boundary requiring sign-off before schema changes and before any Paystack integration code —
Task 16 is both at once.

## Decision

### 1. Payout destination is captured once, as a distributor profile field — not re-entered per request

A new `mobile_money_number` + `mobile_money_network` pair lives on `Distributor` (or a
one-to-one `PayoutAccount`, TBD at implementation time), set via a "Payout Settings" page. A
withdrawal request cannot be submitted until this is filled in — checked the same way
`kyc_status` already gates the request, not re-typed on every request. This avoids the
alternative's real failure mode: a distributor mistyping a mobile money number on one request
sends that request's money to the wrong account with no stored history to catch the typo before
it happens again on the next request.

### 2. `WITHDRAWAL_DAY` gates the payout batch, not the submission window

A distributor can submit a withdrawal request any day (still capped to once per
`WITHDRAWAL_FREQUENCY`). Admin can review/approve any day. A Celery task, mirroring the existing
Binary Bonus / Matching Bonus batch-driver pattern (`apps/commissions/tasks.py`), runs on
`WITHDRAWAL_DAY` and calls Paystack Transfer for every approved-but-unpaid request. Submission and
payout timing are deliberately decoupled — collapsing them (request form only open on Fridays)
would tie the once-per-week cap and the payout cadence into the same constraint for no reason
`SPEC.md` actually requires.

### 3. Wallet is debited on admin approval, not on request or on confirmed payout — with reversal on transfer failure

A withdrawal request holds no funds by itself. The moment an admin approves it,
`apps/wallet/services.py::debit()` runs immediately (inside the same locked/atomic pattern
`consume_paid_starter_pack` already established for placement + PV credit), so the balance can't
be double-spent by a second request approved before the first pays out. If the subsequent Paystack
Transfer later fails or reverses (webhook-reported), a `credit()` reverses the debit
automatically. The rejected alternative — debit only after Paystack confirms success — avoids ever
needing a reversal path, but leaves a real window where two requests could both be approved against
the same starting balance before either pays out, since nothing is held at approval time.

### 4. `AUTO_APPROVE_WITHDRAWALS_ENABLED`/`AUTO_APPROVE_WITHDRAWAL_THRESHOLD` stay unwired

Task 16's withdrawal service never reads either setting. Admin still sees both in Django Admin's
constance settings (removing them is a separate, larger decision about a pre-existing seeded
setting, out of scope for this task), but every request requires an explicit admin approve action
regardless of amount. This is the same "decorative until a task deliberately wires it in" treatment
`BINARY_BONUS_INTERVAL_MINUTES` had before Task 13, except here the setting is never meant to be
wired in at all — `SPEC.md`'s Boundary is explicit and absolute, not a default pending a future
task. A comment at the withdrawal service's admin-approval entry point should say so directly, so a
future contributor doesn't "helpfully" wire it in later without re-reading this ADR.

## Alternatives Considered

### Capture mobile money details fresh on every withdrawal request

- Pros: no new persistent field, no separate "Payout Settings" page to build.
- Rejected: repetitive for the distributor, and — more importantly — a typo on any single request
  sends money to the wrong account with nothing stored to catch it on the next request either.

### Withdrawal request form only open on `WITHDRAWAL_DAY` itself

- Pros: simpler mental model, one fewer moving piece (no separate payout-batch Celery task needed
  if approval triggers payout immediately).
- Rejected: collapses two independent constraints (once-weekly cap, payout cadence) into one for no
  reason `SPEC.md` requires, and removes admin's ability to review/approve at their own pace across
  the week ahead of the payout run.

### Debit wallet only after Paystack confirms the transfer succeeded

- Pros: never needs a reversal/credit-back path, since a debit only ever happens once success is
  certain.
- Rejected: leaves a same-balance double-approval window open between approval and confirmed
  payout — the exact class of bug this project's existing concurrency work
  (`retry_on_lock_contention`, atomic `F()` updates) has consistently treated as unacceptable
  elsewhere in the wallet/commission path.

### Wire `AUTO_APPROVE_WITHDRAWALS_ENABLED` in, gated to a low threshold only

- Pros: uses a setting that already exists instead of leaving it inert.
- Rejected: `SPEC.md`'s Boundary says never auto-approve, "even temporarily for testing
  convenience" — there is no threshold low enough to make this compliant, so wiring it in at all
  isn't on the table.

## Consequences

- **A new Paystack integration surface is required** (`apps/distributors/paystack.py` gains
  Transfer Recipient creation, Transfer initiation, and Transfer-status verification, following the
  same "always re-verify server-side, never trust webhook body alone" pattern
  `verify_transaction`/the Didit integration already established) — this is genuinely new
  attack surface (a new webhook endpoint, a new signature scheme to confirm against Paystack's real
  docs via `source-driven-development`) and needs its own `security-and-hardening` pass, not a
  reuse of the existing charge-side code.
- **The withdrawal request lifecycle needs at least these states**: submitted → approved (debited)
  → queued-for-payout → paid / payout-failed (reversed, re-credited). Admin bulk-approve acts on
  the first transition only; the Friday batch task owns the second onward.
- **A distributor can see an approved request that hasn't paid out yet for up to a week** (approved
  Monday, paid the following Friday) — this is expected behavior under decision 2, not a bug; the
  UI must say so explicitly (e.g. "Approved — payout processes Friday") rather than implying
  approval means immediate payment.
- **Reversal (credit-back) on transfer failure must be idempotent against the same webhook/poll
  retry hazard already solved once for registration payments** (`PendingRegistration`'s idempotent
  consume) — a duplicate failure notification must not double-credit the wallet.

## Doubt-Driven Review (2026-07-22) — 8 gaps found, all resolved before implementation

A `doubt-driven-development` cycle (single-model, fresh-context, adversarial — cross-model
explicitly offered and declined as unnecessary given the findings were already concrete and
grounded in real code) ran against this ADR and the 16a-16h breakdown before any code was written.
It found 8 real gaps, all folded in below rather than deferred. Two were genuine product-policy
questions put to the user directly (same treatment as the four original decisions above); the rest
were engineering-correctness gaps with a single safe answer.

### 5. Wallet balance is checked at both submission and approval, not just implied by min/max

Decisions 1-4 above cover eligibility (KYC, payout destination, amount bounds, once-weekly) but
never stated that the requested amount must also fit the distributor's actual `Wallet.balance` —
`MIN`/`MAX_WITHDRAWAL_AMOUNT` bound a single request in isolation, not against any individual
balance. `submit_withdrawal_request` (16c) must reject a request that exceeds the current balance,
for early distributor-facing feedback. `approve_withdrawal_request` (16d) must re-check it
immediately before calling `debit()` regardless — balance can move between submission and approval
(other requests approved first, a chargeback, etc.) — and if `debit()` raises
`InsufficientBalanceError` at that point, the whole approval transaction rolls back, the request
stays at `submitted` (no new terminal state needed), and the admin sees a clear error rather than a
silent partial-approval.

### 6. `queued_for_payout` is the claim step that prevents a duplicate Paystack Transfer call

The original state list left `queued_for_payout` undefined in practice — nothing ever set it,
which also meant nothing protected against a Celery retry (or a crash between "Paystack accepted
the transfer" and "local status updated") calling `initiate_transfer` a second time for the same
row: a genuine duplicate real-money payout, not just a duplicate local ledger entry. Fixed by
giving `queued_for_payout` a real job: the Friday batch task's first action per request is to
generate and persist a pinned transfer reference and move the row `approved_debited` →
`queued_for_payout` **before** calling `initiate_transfer` — mirroring the pinned-reference pattern
`consume_paid_starter_pack`/`PendingRegistration` already established for the inbound registration
payment. A retry that finds a row already `queued_for_payout` with a reference does not re-claim it
— it resumes by calling `verify_transfer` against the existing reference instead of initiating a
new transfer.

### 7. KYC is re-checked and payout destination is snapshotted at approval time, not read live later

Decision 1 gates initial *submission* on KYC/payout-destination, but nothing previously stated what
happens if either changes during the up-to-a-week window between admin approval and the Friday
payout batch. Two fixes: (a) `approve_withdrawal_request` re-checks `kyc_status` inside the same
locked transaction as the balance check and the debit — if KYC is no longer approved, approval
fails with a clear reason, the same way an insufficient balance does; (b) the payout destination
(`mobile_money_number`/`network`) is copied onto the `WithdrawalRequest` row at the moment it
becomes `approved_debited`, and 16e/16f build the Paystack Transfer Recipient from that **snapshot**,
never by reading live off `Distributor` at Friday-batch time. Without the snapshot, a distributor
editing their payout details (or an account compromise) between approval and the Friday run could
silently redirect an already-debited payout to a different account.

### 8. 16d locks the `Distributor` row first, matching Binary/Matching Bonus's existing order — not a separate `Wallet` lock

The original 16d description said it "locks the `WithdrawalRequest` row and the distributor's
`Wallet` row." Checked against the real code: `process_binary_bonus_for_distributor` and
`process_matching_bonus_for_distributor` (`apps/commissions/services.py`) both lock the
**`Distributor` row first** via `select_for_update_nowait_if_supported`, then call `credit()`
inside that same transaction — `credit()`/`debit()` themselves never take a separate row lock on
`Wallet`, relying instead on an atomic `F()`-relative `.update()`. 16d must follow the identical
order — lock `Distributor` first (needed anyway for the KYC re-check in point 7), then call
`debit()` as-is — rather than introducing a new "lock `Wallet` directly" pattern that could acquire
rows in the opposite order from Binary/Matching Bonus and deadlock against them under real
concurrent load. This was flagged in the original breakdown as a question for 16d's own future
doubt-driven pass to answer; resolving it here instead, before any code exists, is cheaper than
discovering a deadlock under production load.

### 9. `WithdrawalRequestAdmin` hard-locks add/change/delete like `CommissionCycleRunAdmin` after all — corrected 2026-07-22, superseding this section's original resolution

**This section was wrong when first written, and the error was caught building 16b, not by
re-reasoning but by checking two things against real code that should have been checked the first
time.** The original resolution above claimed "this project's admin role is deliberately
`is_staff=True` but not superuser," citing `apps/accounts/management/commands/seed_roles.py` — but
that command is explicitly `DEBUG`-only local-dev seeding, not the real admin design.
`tests/conftest.py`'s `staff_client` fixture (used by every real admin-panel test in this project)
creates its admin test user with `is_superuser=True` and says exactly why: *"Bancostore admin is a
single flat role (SPEC.md 'admin controls every business rule'), not a granular per-model
permission system, so every real admin account is a superuser."* There is no non-superuser admin
scenario in this project's actual design to accommodate — the entire premise of the original "fix"
below was false.

Re-derived properly this time, against Django's real documented behavior (`source-driven-development`,
not assumption) rather than the codebase's own possibly-incomplete precedent:

- `ModelAdmin.has_view_permission()`'s **default implementation independently checks
  `request.user.has_perm()`** for the raw `view_<model>`/`change_<model>` Django permission — it
  does **not** call `self.has_change_permission()`. A superuser passes `has_perm()` for anything
  unconditionally. So hard-locking `has_change_permission` to `False` (unconditionally, no
  superuser exception, exactly `CommissionCycleRunAdmin`/`WalletAdmin`'s existing pattern) **does
  not** break changelist visibility for a real (superuser) admin — the original "Finding K" concern
  was based on a scenario (a non-superuser admin Group needing an explicit `view_withdrawalrequest`
  grant) that doesn't exist here.
- What genuinely *does* break: Django's admin actions, when declared without an explicit
  `permissions` kwarg, default to `allowed_permissions = ("change",)` — meaning the action calls
  **`self.has_change_permission(request)`, my own override**, not a raw superuser bypass. An
  unconditional `return False` there blocks the bulk approve/reject actions 16d needs, for
  *everyone*, superuser included — a real problem, just a different and more precise one than
  originally identified.

**Final resolution:** `WithdrawalRequestAdmin` hard-locks `has_add/change/delete_permission` to
`False` unconditionally, matching `CommissionCycleRunAdmin`/`WalletAdmin` exactly (not
`DistributorAdmin`, which was never a real precedent for this question — it doesn't override any
permission method at all, and its actions work only because every real admin is a superuser and
Django's *unoverridden* default `has_change_permission` passes for superusers trivially). 16d's
`approve_selected`/`reject_selected` actions must each declare
`@admin.action(..., permissions=["view"])` explicitly, per
[Django's admin actions documentation](https://docs.djangoproject.com/en/5.0/ref/contrib/admin/actions/)
("If `permissions` has more than one permission, the action will be available as long as the user
passes at least one of the checks" — `"view"` maps to `has_view_permission()`, which resolves `True`
for a superuser independent of the hard-locked `has_change_permission`). This gives
`WithdrawalRequest` the same full lockdown as the other pure-audit admins, plus working bulk actions,
with no dependency on a non-superuser admin scenario that was never real.

### 10. Values are locked in at submission, not re-read live at approval (user-confirmed)

The tax amount and eligibility bounds were already computed and shown to the distributor before
they confirmed submission (per the existing "shown before confirming" requirement) — changing the
deal afterward without telling them would be misleading. `WithholdingTaxRate`/min/max are read once
at `submit_withdrawal_request` time and stored on the `WithdrawalRequest` row; that stored figure is
authoritative through approval and payout, even if admin changes the live constance setting in the
meantime. A later admin change to `MAX_WITHDRAWAL_AMOUNT` never retroactively invalidates an
already-submitted request.

### 11. A rejected or reversed request frees the weekly slot back up (user-confirmed)

A rejection (bad payout details, a fraud flag later cleared, etc.) must not cost the distributor
their whole `WITHDRAWAL_FREQUENCY` window — only a request still `submitted`, `approved_debited`,
`queued_for_payout`, or `paid` counts against the once-per-window cap.
`submit_withdrawal_request`'s window check excludes `rejected` requests. By the same reasoning
(the user's principle was "don't penalize the distributor for an outcome outside their control"),
a `payout_failed_reversed` request is excluded too — a Paystack-side transfer failure is not the
distributor's fault either, and it did not result in a completed payout.

### 12. `WITHDRAWAL_FREQUENCY` needs an explicit string-to-duration mapping

`WITHDRAWAL_FREQUENCY` is stored as the bare constance string `"weekly"` with no typed widget
(unlike `WITHDRAWAL_DAY`/`MIN`/`MAX_WITHDRAWAL_AMOUNT`, which all got dedicated typed widgets in the
same pass that resolved Open Question #5) — `T + "weekly"` is not a valid operation. This does not
need a new constance widget or schema change; `apps/withdrawal/services.py` defines a small
`{"weekly": timedelta(days=7)}`-shaped mapping and looks the live setting value up in it at
window-check time. If a value outside the mapping is ever configured, the check must fail loudly
(raise), not silently treat it as "no window restriction."

## Task 16a Schema Review (2026-07-22) — flat fields on `Distributor`, 7 gaps closed

A second `doubt-driven-development` cycle, scoped narrowly to 16a's own open question (flat fields
on `Distributor` vs. a separate `PayoutAccount` model), ran before writing the migration. It found
7 real gaps — including one that traced back to this ADR itself silently narrowing SPEC.md's own
stated scope.

**Flat fields on `Distributor`, not a separate model — reasoned explicitly this time, not just
asserted.** `DiditVerification` was kept separate for three specific reasons (a dozen-plus
columns; Didit's externally-sourced structured response; informational-only data that must never
become authoritative by itself) — none apply here. Two scalar fields describing a distributor's
own payout destination are structurally closer to `phone_number` (a core, directly-owned identity
attribute already flat on the model) than to KYC's vendor-response shape. A separate model would
be speculative future-proofing (e.g. "in case a distributor needs multiple payout methods later")
with no evidence that need exists now — the same over-engineering this project's conventions
already reject elsewhere.

**Scope check against `SPEC.md`'s own wording.** `SPEC.md`'s Objective line describes the platform
moving money via "Mobile Money/**bank** payouts" — this ADR's Decision 1 had silently narrowed that
to mobile-money-only without flagging the discrepancy. Put to the user directly: **mobile money
only for Task 16's MVP**, bank-account payout is a real, recorded Phase 2 item (not silently
dropped) — revisit if/when Phase 2 scoping happens. `WithdrawalRequest` and `Distributor`'s payout
fields are mobile-money-shaped only; a future bank-payout addition is a new field set and a new
Paystack recipient type, not a rename of these.

**Field definition, corrected:**
```python
class Distributor(models.Model):
    class MobileMoneyNetwork(models.TextChoices):
        MTN = "mtn", "MTN MoMo"
        TELECEL = "telecel", "Telecel Cash"
        AIRTELTIGO = "airteltigo", "AirtelTigo Money"
    ...
    mobile_money_number = PhoneNumberField(blank=True, default="")
    mobile_money_network = models.CharField(
        max_length=20, choices=MobileMoneyNetwork.choices, blank=True, default=""
    )
```
Three fixes from the original draft: (1) `blank=True, default=""` on both fields, not
`null=True, blank=True` on one and `blank=True, default=""` on the other — every other optional
field on `Distributor` (`kyc_rejection_reason`, `full_name`, `rank`, etc.) uses the same convention,
and mixing `None`/`""` for one logical "not set yet" state was an inconsistency with no stated
reason; (2) `MobileMoneyNetwork` nested inside `Distributor`, matching `KycStatus`/
`DiditVerification.Status`'s existing in-model-class convention instead of sitting ambiguously at
module level; (3) **"Vodafone" corrected to "Telecel"** — Vodafone Ghana rebranded to Telecel Ghana
and Paystack's own mobile money support docs confirm "Telecel Cash (previously Vodafone Cash)" as
the current network name (verified via `source-driven-development`-style live check before writing
the choices, not assumed from training-data-era knowledge — see
[Paystack: Pay with Mobile Money](https://support.paystack.com/en/articles/2128386)). The exact
Paystack `bank_code` each network maps to for Transfer Recipient creation is intentionally **not**
guessed here — that's 16e's job, grounded in Paystack's real "List Banks" (`type=mobile_money`)
response at implementation time, not hardcoded from a web search now.

**Both-or-neither constraint added.** A `CheckConstraint` (mirroring `Wallet`'s
`balance__gte=0` precedent from Task 15 — the same non-speculative "cheap invariant, real defense
in depth" reasoning) enforces that `mobile_money_number` and `mobile_money_network` are either both
empty or both set, so a partial save (Django Admin edit, a bug in a future form) can't silently
leave one populated without the other.

**No uniqueness constraint on `mobile_money_number` (user-confirmed).** Shared household mobile
money wallets are common and legitimate in Ghana; a hard per-distributor uniqueness constraint
would reject genuine distributors over a real, non-fraudulent pattern. Sponsor/downline collusion
via a shared payout destination is a real MLM fraud vector, but the fix for that is admin-facing
monitoring/reporting (out of scope for 16a, not silently solved by a schema constraint that would
also block legitimate use), not a field-level block.

**`django-simple-history` coverage is sufficient, now documented rather than an unexamined side
effect.** `Distributor.history = HistoricalRecords()` already tracks every field, so payout-
destination changes land in `HistoricalDistributor` automatically — deliberately useful here, since
a payout-destination change shortly before a withdrawal is a real fraud signal worth having in the
audit trail. A code comment states this explicitly (mirroring how every other non-obvious
history implication on this model already has one), rather than leaving a future reader to
rediscover it.

**No dedicated verification step for `mobile_money_number` in 16a (deliberate phasing, not a silent
gap).** Unlike `phone_number`/`phone_verified` (backed by an existing SMS-OTP flow), 16a adds no
equivalent for the payout number — building one now would mean a second OTP-adjacent flow
(new scope, not derivable from the task) before any Paystack integration exists to actually need
it. Real verification happens naturally in 16e: Paystack's Transfer Recipient creation resolves
the account and would surface an invalid number at that point. If that turns out to be too late in
practice (e.g. a distributor only discovers a typo after admin approval, not before), revisit —
flagged here rather than silently assumed sufficient.

## Task 16d Design Review (2026-07-23) — lock ordering, idempotency, and a real double-debit gap closed before implementation

A `doubt-driven-development` pass against `approve_withdrawal_request`/`reject_withdrawal_request`'s
design (before either existed in code) found two serious gaps and one deliberate divergence from
existing precedent, all resolved before implementation.

**The status check must read a locked, fresh row — not a snapshot taken before any lock was held.**
The original draft checked `WithdrawalRequest.status` against a plain, unlocked `.get()` performed
*before* `Distributor` was locked. Two admins approving the same request in quick succession (a
realistic scenario — two staff members both working the same filtered changelist, not just a
same-instant double-click) could both pass that stale check: the second admin's `Distributor` lock
acquisition would only contend with the first if their transactions genuinely overlapped, which
isn't guaranteed. The actual double-debit protection would then fall entirely to
`apps.wallet.models.WalletTransaction`'s own unique `(wallet, reference, transaction_type)`
constraint, several call-frames away in a different app — surfacing as an undocumented raw
`IntegrityError` (or, depending on incidental balance state, `InsufficientBalanceError`) instead of
a clean, catchable exception here. Fixed: `approve_withdrawal_request` now locks `Distributor` first
(preserving point 8's existing order below, unchanged), then locks the `WithdrawalRequest` row
itself *before* checking status — so the check reads committed, visible data, not a pre-transaction
memory of it.

**`reject_withdrawal_request` had zero locking at all — the one outlier in this codebase's write-
path convention.** Every other mutating function in this project (`submit_withdrawal_request`,
`credit`/`debit`, `approve_kyc`/`reject_kyc`, the binary/matching bonus batch drivers) wraps its
check-then-write in `transaction.atomic()` with a locked row. The original `reject_withdrawal_
request` draft was a bare `.get()` → status check → four field assignments → full-row `.save()`,
with nothing serializing it against a concurrent `approve_withdrawal_request` on the same row. Under
the same "two admins, same changelist" scenario: if admin A approves (debits the wallet, sets
`status`/`payout_mobile_money_number`/`payout_mobile_money_network`/`reviewed_by`/`reviewed_at`,
commits) while admin B's stale in-memory `reject_withdrawal_request` call is still in flight, B's
blind `.save()` would silently revert every field A just committed back to B's stale values —
`status=REJECTED` while the wallet had already been debited, a real accounting inconsistency, not
just a display glitch. Fixed: `reject_withdrawal_request` now locks the `WithdrawalRequest` row
inside `transaction.atomic()` before reading or writing anything, the same discipline as everywhere
else in this codebase. It never locks `Distributor` — there's nothing here that touches the wallet
or the distributor row, so no new lock-ordering question is introduced.

**`WithdrawalRequestNotPending` raises rather than silently no-oping — a deliberate divergence from
`approve_kyc`'s existing idempotent-no-op precedent, not an oversight.** `apps.distributors.services
.approve_kyc` treats "already approved" as a silent early-return. For a money-moving action, that
precedent was rejected: a bulk admin action's results summary should be able to tell an admin
"already approved by someone else" apart from "approved just now," not silently treat a stale action
as a success. The exception carries a structured `status` attribute (the request's actual current
status) specifically so `apps/withdrawal/admin.py`'s bulk-action loop can build that distinction
without parsing a message string.

**Both re-checks happen inside the lock, not just `kyc_status`.** The original draft only
re-checked `kyc_status` (matching what this ADR's earlier section already called for) but not
`has_payout_destination` — a distributor could clear their payout profile between submission and
approval, and the model's own both-or-neither `CheckConstraint` explicitly *permits* both snapshot
fields landing as empty strings, so nothing would have caught it. `approve_withdrawal_request` now
re-checks both.

**Follow-up `code-review-and-quality` pass (2026-07-23) after implementation** found one real gap
in the admin bulk-action loop: `WithdrawalRequestNotFound` was documented on both service functions
but never actually caught in `apps/withdrawal/admin.py` — an uncaught exception mid-loop would 500
the *entire* bulk action, losing the results summary for every row already processed in the same
request (even though each row's own approval/rejection is independently atomic and stays
committed). Fixed in both `approve_selected` and `reject_selected`, with a test simulating the
"row vanished mid-batch" case via monkeypatch (isolating the admin loop's own error-handling from
the deeper service-layer locking already proven by the concurrency test).

**Deferred, not silently skipped:** the same review flagged that a very large bulk selection
(hundreds+ of requests) processes synchronously within one HTTP request, with no selection-size cap
and no background-job offload — each row's own atomic lock+check+debit is necessary and can't be
batched into fewer queries, so this isn't a fixable N+1, just an unbounded-request-duration risk.
This mirrors the existing KYC bulk-action's own scale ceiling exactly, so it isn't a regression, but
it's worth a deliberate decision (a selection-size cap, or moving to Celery) before withdrawal
review volume ever gets large enough to matter for a money-moving action specifically -- flagged
here rather than silently inherited.
