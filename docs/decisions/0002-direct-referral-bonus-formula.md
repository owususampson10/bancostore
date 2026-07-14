# ADR-0002: Direct Referral Bonus = rate x PV, not rate x price paid

## Status

Accepted (2026-07-14)

## Date

2026-07-14

## Context

Task 12 (Direct Referral Bonus) needed a formula: how much does a sponsor earn when their
personally-referred distributor buys a starter pack? `DIRECT_REFERRAL_BONUS_RATE` is a constance
percentage (default 10%). The open question was what it's a percentage *of* — the PV the purchase
generates, or the GHS price the distributor actually paid.

Two source documents disagreed, and neither was internally consistent:

- The original requirements doc (`docs/Bancostore_Features_and_Workflow_v4.docx`, Section 14's
  Kofi/Ama walkthrough) states: "He selects Starter Pack B (GHS 2,000) and pays... 1,000 PV is
  added to Ama's right leg. **Ama receives a GHS 200 referral bonus instantly (10% of 1,000 PV).**"
  It also states: "Each friend buys Pack A (GHS 1,500 = 500 PV). **Kofi earns GHS 150 in referral
  bonuses (2 x GHS 75).**"
- `tasks/todo.md`'s own Task 12 planning note (written before this ADR, during earlier scoping)
  said the acceptance test should prove **GHS 100** for the identical Pack B scenario — directly
  contradicting Section 14's stated GHS 200 for the same purchase.

Working the arithmetic both ways against Section 14's own two examples:

| Purchase | Price | PV | Section 14's stated bonus | 10% x price | 10% x PV |
|---|---|---|---|---|---|
| Pack B (Kofi, referred by Ama) | GHS 2,000 | 1,000 | GHS 200 | **200** (matches) | 100 (off by 2x) |
| Pack A (friend, referred by Kofi) | GHS 1,500 | 500 | GHS 75 | 150 (off by 2x) | **50** (off by 1.5x) |

No single formula — "10% of price" or "10% of PV" — reproduces both of Section 14's own numbers at
once. The Pack B case favors "price"; the Pack A case doesn't match either formula cleanly. This
means Section 14's worked examples contain at least one arithmetic error in the original document,
not a discoverable-by-re-reading ambiguity. `doubt-driven-development` scrutiny (working the actual
numbers instead of trusting the doc's prose) surfaced this before any code was written.

## Decision

**Direct Referral Bonus = `DIRECT_REFERRAL_BONUS_RATE` x PV, treating 1 PV as GHS 1** for this
calculation specifically (PV and price are not the same currency-equivalent scale elsewhere in the
system — Pack A is 3 GHS/PV, Pack B is 2 GHS/PV — this decision applies only to how the referral
bonus is computed, not a general PV-to-GHS conversion rate).

Confirmed values at the default 10% rate:
- Pack A (500 PV) = **GHS 50**
- Pack B (1,000 PV) = **GHS 100**

Implemented in `apps/commissions/services.py::calculate_direct_referral_bonus`, rounded to the
pesewa (`ROUND_HALF_UP`) — the first money-rounding convention established in this codebase.

## Alternatives Considered

### Rate x price paid

- Pros: Matches Section 14's stated Pack B bonus (GHS 200) exactly.
- Cons: Produces GHS 150 for the Pack A case, not Section 14's stated GHS 75 — off by exactly 2x.
  Also couples the bonus to the retail price, which can change independently of PV (Pack A and Pack
  B already have different GHS-per-PV ratios), making the bonus less predictable for admins tuning
  `DIRECT_REFERRAL_BONUS_RATE` in isolation.
- Rejected: doesn't reconcile with Section 14's own second example any better than the chosen
  formula does with the first, and couples the bonus to price instead of the PV value the rest of
  the commission engine (Binary Bonus, Matching Bonus) will be built around.

### Treat Section 14's numbers as authoritative and back-solve a per-pack fixed bonus

- Pros: Would match the doc's narrative numbers exactly (GHS 200 / GHS 75) without needing a single
  formula.
- Cons: Not actually a formula — would require a hardcoded bonus amount per starter pack tier,
  contradicting the spec's own framing that referral bonus is "10% of new recruit's PV," and
  breaking the moment a new pack tier is added or `DIRECT_REFERRAL_BONUS_RATE` is changed via
  constance (the entire point of making it admin-configurable).
- Rejected: this project's rule is "business rules live in settings, not code" (`CLAUDE.md`) — a
  fixed per-pack bonus can't be tuned by an admin the way a rate can, and it's not actually resolving
  the contradiction, just picking one side of it and hardcoding it.

## Consequences

- **Section 14's narrative numbers (GHS 200 for Ama, GHS 75 per Kofi referral) are now known errors
  in the original requirements doc, superseded by this decision.** Do not "correct" the code to
  match Section 14 if it's consulted again for Task 12 or reused as a reference for Tasks 13/14 —
  see the `project_direct_referral_bonus_formula` memory.
- **Sets precedent for how PV is treated in Tasks 13/14** (Binary Bonus, Matching Bonus): both are
  also specified as a percentage applied to PV in `SPEC.md`'s Section 15 rule table ("Binary Bonus
  Rate: 7.5% of weak leg PV," "Matching Bonus: 5% of downline binary earnings"). The same "1 PV =
  GHS 1 for commission math" convention this ADR establishes should be assumed consistent going
  into those tasks unless a similar contradiction turns up in their own worked examples — which
  should get the same scrutiny this decision did, not be assumed correct by default.
- **This was a user decision, not a spec lookup.** The formula could not be derived by reading either
  source document more carefully — both are self-inconsistent. This ADR exists specifically because
  `CLAUDE.md`'s boundary rules require asking first before "changing seeded default commission
  rates/caps/fees or their formulas," and this was the first time that boundary was actually
  exercised in this project.
