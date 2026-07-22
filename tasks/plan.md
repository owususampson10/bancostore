# Implementation Plan: Bancostore MVP

## Overview

Build the MVP slice defined in `SPEC.md`: a Ghana ecommerce + binary-MLM platform on Django 5 /
Python 3.12 / MySQL / Redis / Paystack. Work is broken into small, single tasks. Every task is a
full vertical slice (database + backend + frontend together for that one piece of functionality),
tested on both backend and frontend, and verified working before the next task starts — per the
agreed build process.

**Stack note:** the original plan used Laravel/PHP. That was replaced with Django/Python after
Laravel/PHP couldn't be made to run on this Mac (macOS 12.7.6 Monterey) and the Oracle Cloud
remote-server fallback stalled on phone verification — see `SPEC.md` Tech Stack for the full
rationale and the Django-equivalent of every Laravel/Filament/Livewire piece named below.

## Architecture Decisions

- **Binary tree = closure table (`binary_tree_edges`), not parent-pointer recursion.** Subtree PV
  reads must be O(log n)/O(1), not a walk of the whole downline. Built in Task 9, not deferred —
  see `SPEC.md` Scale Architecture.
- **PV/commission is event-driven.** Every purchase writes to a `pv_ledger` table and increments
  every ancestor's leg totals at write time (Task 10). The 10-minute Binary Bonus Celery task
  (Task 13) only reads current aggregates — it never re-walks the tree.
- **All business-rule values (rates, caps, fees) come from `django-constance` config**, seeded
  from `SPEC.md`/`docs/` Section 13/15 values (Task 3). Never hardcoded in a service or task.
- **Money is integer/pesewa-safe everywhere** (`Decimal`) in the commission/wallet/withdrawal
  path — no floats.
- **App servers are stateless** — sessions/cache through Redis from the first scaffold (Task 1),
  so the documented VPS scale-path requires no later code changes.
- **Single `User` model + role via Django groups/permissions**, with a separate `Distributor`
  model for distributor-only fields (IR ID, rank, KYC status, sponsor). Keeps regular-customer
  auth simple while giving distributors their own schema surface.

## Task List

### Phase 0: Foundation
- [x] Task 1: Scaffold the Django 5 project and install the full stack
- [x] Task 2: Roles & permissions (Customer / Distributor / Admin)
- [x] Task 3: Settings foundation — seed all MVP-relevant business rules

**Checkpoint A:** `pytest` passes, `black --check . && ruff check .` clean, seeded
admin/customer/distributor stub users exist, settings values are readable from the database.

### Phase 1: Authentication
- [x] Task 4: Regular customer registration & login (email/phone, Google, guest checkout flag)
- [x] Task 5: Distributor registration & login (phone + SMS OTP) — mNotify, fully verified end to end with a real SMS and real login
- [x] Task 6: Admin login with mandatory 2FA — backend + all 6 real Stitch screens built and verified live end to end (login, forced TOTP setup, lockout, returning-login code entry)

**Checkpoint B:** all three roles can register and log in end-to-end, with passing tests.

### Phase 2: Catalog & Storefront
- [x] Task 7: Product model + Django Admin CRUD (photos, price, PV, category, variants, stock)
- [x] Task 8: Public storefront — home page, product listing/detail, search/filter/sort
  (split 2026-07-12 into 8a: site shell + home, 8b: listing with HTMX search/filter/sort,
  8c: product detail — see `tasks/todo.md`)

**Checkpoint C — verified 2026-07-12:** admin adds a product in Django Admin; it appears correctly
on the public storefront (home, listing with working search/filter/sort, detail page).

### Phase 3: Distributor Core Domain (MLM)
- [x] Task 9a: Binary tree closure table schema (`binary_tree_edges`, `pv_ledger`)
- [x] Task 9b: Placement + spillover service (sponsor picks leg, auto-balance fallback,
  in-leg shallowest-first spillover — confirmed with user 2026-07-13)
- [x] Task 9c: Ancestor-aggregate query service (O(log n)/O(1), feeds Task 13)
- [x] Task 10a: Registration form + sponsor validation (no payment)
- [x] Task 10b: Registration fee payment (Paystack — Boundary) — creates the account
- [x] Task 10c: Starter pack selection + payment (Paystack — Boundary) — sets rank
- [x] Task 10d: Binary tree placement + PV ledger update on starter-pack confirmation
- [x] Task 11a: Didit verification model + API client (revised 2026-07-13 -- replaces the
  self-hosted upload form shipped in commit `ebf8e22` with Didit's hosted ID+selfie verification
  flow, a Boundary-tier external integration like Paystack; see tasks/todo.md for the full
  rationale and the old-code removal step)
- [x] Task 11b: Didit verification flow end-to-end (session creation, callback, webhook,
  idempotent re-verify-server-side consume)
- [x] Task 11c: Admin KYC review + IR ID generation on approval (Didit's result is informational
  only -- admin always makes the final call, never auto-approved, per SPEC.md's Boundary;
  concurrency-safe IR ID sequence needs a doubt-driven-development pass before implementing;
  IR ID generation moved from Task 10 to match Section 14's exact order, confirmed 2026-07-13)

**Checkpoint D — verified 2026-07-14, Phase 3 complete:** a new distributor can register, pay the
registration fee, choose and pay for a starter pack, end up correctly placed in the binary tree
with ancestor PV ledgers updated, complete Didit's hosted KYC verification (ID + selfie), and pass
admin review to receive a permanent IR ID — verified end to end through Paystack test mode and
Didit's API (mocked HTTP), reproducing Section 14 steps 4–9.

### Phase 4: Commission Engine (elevated test rigor) — complete 2026-07-22
- [x] Task 12: Direct Referral Bonus — instant credit on starter pack purchase
- [x] Task 13: Binary Bonus task — every 10 minutes, weak-leg, carry-forward, expiry, weekly cap
  (core math + Celery Beat batch driver both shipped; batch driver went through a dedicated
  `security-and-hardening` pass after a real silent-PV-credit-loss bug was caught by real MySQL in
  CI — see `tasks/todo.md` Task 13's 13g–13i notes and `CLAUDE.md`)
- [x] Task 14: Matching Bonus task — weekly, 3-level Bronze / unlimited Silver (built via
  `spec-driven-development` → `planning-and-task-breakdown` → TDD → `doubt-driven-development` →
  parallel `security-and-hardening`/`code-review-and-quality` → `code-simplification`, then a
  CodeRabbit-reviewed PR before merge; see `tasks/todo.md` Task 14's notes and `CLAUDE.md`)

**Checkpoint E — closed 2026-07-22.** `tests/feature/commissions/test_full_commission_journey.py`
chains registration → referral bonus (twice — Efua earns from GrandRoot's purchase, GrandRoot earns
from Kofi's and Ama's) → real binary tree placement + write-time PV credit → `calculate_binary_bonus()`
→ `calculate_matching_bonus()`, through the real service/task functions (not the HTTP/webhook layer,
already covered by `tests/feature/distributors/`), asserting the exact resulting commission numbers
at every step. Not a literal reproduction of Section 14's own narrative figures — its Direct Referral
Bonus numbers (GHS 200 / GHS 75) are documented, confirmed contradictions in the source doc (see
`CLAUDE.md`); uses this project's already-resolved formula (rate × PV) instead, same as every other
test in this codebase. Also proves `sum_downline_binary_bonus_earnings`'s transaction-type filter
holds under a real, mixed-transaction-type wallet history (Efua's matching bonus correctly excludes
GrandRoot's GHS 150 of direct-referral earnings, counting only her GHS 37.50 binary bonus) and that
matching bonus requires actual downline binary-bonus earnings, not just a qualifying rank
(GrandRoot herself earns no matching bonus — her downline never earned a binary bonus).

### Phase 5: Wallet & Withdrawal
- [ ] Task 15: Wallet ledger — credit/debit entries, balance, earnings history view
- [ ] Task 16: Withdrawal request flow — tax deduction, Paystack payout, admin approval —
  **needs min/max withdrawal amount + withdrawal day decision**

**Checkpoint F:** a distributor requests a withdrawal, tax is deducted correctly, admin approves,
and a simulated Paystack payout succeeds.

### Phase 6: Checkout & Orders
- [ ] Task 17: Cart + checkout — delivery/pickup, delivery-fee-by-zone, Paystack payment —
  **needs delivery zone fee table decision**
- [ ] Task 18: Order status lifecycle + notifications + admin order management

**Checkpoint G:** both a customer and a distributor complete a purchase; PV is generated only for
the distributor purchase; order status updates correctly.

### Phase 7: Cooling-Off Refund
- [ ] Task 19: 7-day cooling-off refund — processing fee, PV reversal, commission reversal

### Phase 8: Distributor Dashboard (real-time)
- [ ] Task 20: Dashboard core stats (wallet, earnings, team size, leg PV, rank, referral link)
- [ ] Task 21: Binary tree visual view, earnings history, carry-forward tracker, notification bell

**Checkpoint H:** dashboard values update live (no page refresh) when a commission is credited in
another session — proves Django Channels real-time wiring works.

### Phase 9: Admin — Remaining MVP Pieces
- [ ] Task 22: Admin user/distributor management (search, profile, suspend/deactivate)
- [ ] Task 23: Admin commission oversight (per-distributor view, weekly-cap hits, tree view)

**Checkpoint I (final MVP checkpoint):** the full Section 14 distributor journey works end to end
through the UI. All pytest tests pass, `black`/`ruff` clean. Verify against `SPEC.md` Success
Criteria.

### Phase 10: Deployment
- [ ] Task 24: Deploy to Hostinger VPS (production) — **needs Hostinger KVM 2 VPS provisioned
  first, and a CI provider confirmed (Open Question #1) before this task starts**

**Checkpoint J (go-live):** Bancostore is reachable over HTTPS at the production domain, running
on the Hostinger VPS against real MySQL, with Celery/Celery Beat/Daphne kept alive by Supervisor
and surviving a server reboot.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Wrong commission formula | High — real money miscalculated | Elevated test rigor in Phase 4 (Task 12–14); every edge case in `SPEC.md` Testing Strategy covered before merge |
| Binary tree designed naively | High — costly retrofit at scale later | Closure table + event-driven ledger built in Task 9–10, not deferred |
| Missing concrete config values (SMS/email provider, delivery zones, withdrawal limits) | Medium — blocks specific tasks | Flagged per-task below; ask before starting that task, not before the whole plan |
| No Paystack/SMS sandbox account yet | Medium — blocks Tasks 5, 10, 16, 17 | Ask-first boundary already in `SPEC.md`; confirm account access before those tasks |
| Homebrew `mysql` doesn't install on this Mac (confirmed — no bottle for macOS 12, source build hits a linker failure) | Resolved | Local dev uses SQLite instead; MySQL only runs in CI (GitHub Actions, Linux, no compile issue) and production (Hostinger VPS, purchased later). See `SPEC.md` Local dev environment. |
| SQLite's single-writer locking hides concurrency bugs in PV-ledger/wallet writes that only show up under real MySQL | Medium — could ship a bug that only appears in production | CI must run the commission/wallet/withdrawal suite against a real MySQL service container, not SQLite — see `SPEC.md` Testing Strategy |
| Task touches more than one subsystem | Low — scope creep mid-task | Vertical slices kept to db+backend+frontend for one feature only; split further if a task needs "and" in its title |

## Open Questions (carried from `SPEC.md`, mapped to blocking tasks)

1. CI provider (GitHub Actions assumed) — not blocking; resolve before Checkpoint I / deployment
2. ~~SMS provider, Arkesel or Hubtel~~ — resolved 2026-07-11: **mNotify**, API key in hand
3. ~~Email provider, Mailgun or Gmail SMTP~~ — resolved 2026-07-10: **Gmail SMTP**, verified with
   a real send (Task 4); revisit for Mailgun before real production volume
4. Delivery zone fee table + free-delivery threshold — **needed before Task 17**
5. Min/max withdrawal amount + withdrawal day — **needed before Task 16**
6. Existing Paystack/mNotify accounts, or need to create them — **needed before Tasks 5, 10, 16,
   17**. mNotify: confirmed. Paystack: still open.
7. ~~Confirm `pyenv` Python + Homebrew `mysql`/`redis` actually install cleanly on this Mac~~ —
   resolved: Python (already present, no `pyenv` needed) and Redis installed cleanly; MySQL did
   not, so local dev uses SQLite instead (see `SPEC.md` Local dev environment)
8. Wire up GitHub Actions CI with a real MySQL service container once a CI provider is confirmed
   (Open Question #1) — this is the safety net for the SQLite/MySQL concurrency gap above, needed
   before Phase 4 (Commission Engine) merges anything, not blocking Task 1

Each of these will be asked as a short question right before its blocking task starts, rather than
all at once now.
