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
- [ ] Task 2: Roles & permissions (Customer / Distributor / Admin)
- [ ] Task 3: Settings foundation — seed all MVP-relevant business rules

**Checkpoint A:** `pytest` passes, `black --check . && ruff check .` clean, seeded
admin/customer/distributor stub users exist, settings values are readable from the database.

### Phase 1: Authentication
- [ ] Task 4: Regular customer registration & login (email/phone, Google, guest checkout flag)
- [ ] Task 5: Distributor registration & login (phone + SMS OTP) — **needs SMS provider decision**
- [ ] Task 6: Admin login with mandatory 2FA

**Checkpoint B:** all three roles can register and log in end-to-end, with passing tests.

### Phase 2: Catalog & Storefront
- [ ] Task 7: Product model + Django Admin CRUD (photos, price, PV, category, variants, stock)
- [ ] Task 8: Public storefront — home page, product listing/detail, search/filter/sort

**Checkpoint C:** admin adds a product in Django Admin; it appears correctly on the public
storefront.

### Phase 3: Distributor Core Domain (MLM)
- [ ] Task 9: Binary tree schema (closure table + `pv_ledger`) and placement/spillover service
- [ ] Task 10: Distributor onboarding — registration fee, starter pack, tree placement, IR ID
- [ ] Task 11: KYC submission (distributor) + review/approve (admin)

**Checkpoint D:** a new distributor can register, pay, get placed with correct spillover, and pass
KYC to receive an IR ID — verified end to end.

### Phase 4: Commission Engine (elevated test rigor)
- [ ] Task 12: Direct Referral Bonus — instant credit on starter pack purchase
- [ ] Task 13: Binary Bonus task — every 10 minutes, weak-leg, carry-forward, expiry, weekly cap
- [ ] Task 14: Matching Bonus task — weekly, 3-level Bronze / unlimited Silver

**Checkpoint E:** the Section 14 example journey's exact commission numbers (e.g. GHS 200 referral,
GHS 37.50 binary bonus) reproduce in a feature test.

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
2. SMS provider, Arkesel or Hubtel — **needed before Task 5**
3. Email provider, Mailgun or Gmail SMTP — **needed before Task 4** (password reset emails)
4. Delivery zone fee table + free-delivery threshold — **needed before Task 17**
5. Min/max withdrawal amount + withdrawal day — **needed before Task 16**
6. Existing Paystack/Arkesel/Hubtel accounts, or need to create them — **needed before Tasks 5, 10, 16, 17**
7. ~~Confirm `pyenv` Python + Homebrew `mysql`/`redis` actually install cleanly on this Mac~~ —
   resolved: Python (already present, no `pyenv` needed) and Redis installed cleanly; MySQL did
   not, so local dev uses SQLite instead (see `SPEC.md` Local dev environment)
8. Wire up GitHub Actions CI with a real MySQL service container once a CI provider is confirmed
   (Open Question #1) — this is the safety net for the SQLite/MySQL concurrency gap above, needed
   before Phase 4 (Commission Engine) merges anything, not blocking Task 1

Each of these will be asked as a short question right before its blocking task starts, rather than
all at once now.
