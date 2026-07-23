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
- [x] Task 15: Wallet ledger — credit/debit entries, balance, earnings history view. Built via
  `spec-driven-development` (resolved a stale "Files likely touched" pointer and the empty-vs-
  signed-amount design) → `planning-and-task-breakdown` (15a–15f) → TDD per slice, then dedicated
  `security-and-hardening` and `code-review-and-quality` passes before the PR: the security pass
  caught a `WalletAdmin` gap where a staff account could cascade-delete a distributor's entire
  ledger via Django Admin's default delete action (fixed, mirroring `CommissionCycleRunAdmin`'s
  full lockdown) and a missing role guard that crashed non-distributor accounts with a 500 instead
  of a 403 (fixed); the review pass caught an unstable pagination sort with no tie-breaker (fixed).
  Two follow-up rounds after merge, both caught by the user reviewing the live page rather than by
  any test: a missing `<script src=".../main.js">` tag silently broke the sidebar's collapse
  toggle (Alpine.js never loaded — found and fixed via a real headed-browser Playwright check, not
  guessed at); and several elements from the approved Stitch design had been silently simplified
  away on first build (the real brand logo, header notification icon + page label, a "Withdraw
  Now" CTA, a last-withdrawal-date caption) — restored, plus keyboard-accessible hover popovers
  added for the collapsed sidebar's icon-only nav. PDF/CSV export was raised and confirmed
  out of scope per `SPEC.md`'s MVP scope section, not silently skipped. See `tasks/todo.md` Task
  15's full notes and `CLAUDE.md`.
- [ ] Task 16: Withdrawal request flow — tax deduction, Paystack payout, admin approval — **next
  up; unblocked as of 2026-07-22 (Open Question #5 below resolved)**. Scoped 2026-07-22 via
  `spec-driven-development` (four money-safety design questions with no answer in `SPEC.md` — payout-
  destination capture, `WITHDRAWAL_DAY` semantics, wallet debit timing, the `AUTO_APPROVE_*`
  settings' fate against a hard Boundary — put to the user directly rather than assumed, resolved
  in `docs/decisions/0004-withdrawal-payout-design.md`) → `planning-and-task-breakdown` (16a–16h,
  see `tasks/todo.md`). **16e/16f (Paystack Transfer API wrapper + webhook) each need explicit
  user sign-off before starting**, independent of this breakdown already being reviewed — `SPEC.md`'s
  Boundary on Paystack integration code applies per change, not once per task.

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

### Phase 9: Admin Portal — custom Stitch-designed dashboard (redefined 2026-07-23)
**Scope change from Django Admin to a full custom dashboard, decided 2026-07-23:** the admin user
is not software-literate — every screen an admin touches routinely needs to feel like part of the
Bancostore application, not Django's generic admin panel. This plan reflects that decision, but
`SPEC.md`'s "Admin panel | Django Admin (customized)" tech-stack line is **still the authoritative
record and has not been updated to match** — that edit is intentionally held pending explicit
confirmation (per this project's own convention of asking before editing `SPEC.md`/`CLAUDE.md`),
not silently assumed. Treat this phase's scope as tentative, not adopted, until that update lands.
Folds the original Task 22/23 scope into this initiative rather than building them as raw Django
Admin.

**Design principle:** Django Admin's `ModelAdmin` classes (KYC review — Task 11, wallet/commission
audit — Tasks 13-15, withdrawal approval — Task 16d) stay exactly as-is underneath — same
permissions, same `simple_history` audit trails, same hard lockdowns. New Stitch-designed screens
sit in front and call the same already-tested service functions (`approve_kyc`/`reject_kyc`,
`approve_withdrawal_request`/`reject_withdrawal_request`, etc.) so none of the money-safety/
concurrency work already through doubt-driven review gets re-litigated — only the presentation
layer changes. Django Admin stays reachable underneath as a technical fallback.

**Architecture:** one dedicated `apps/admin_portal/` app with its own branded shell template
(mirrors `templates/distributors/base_dashboard.html`'s sidebar shell), rather than scattering
admin-facing views across each domain app. Reuses the existing admin login/2FA flow (Task 6,
already Stitch-styled) — only changes where login lands and what nav the admin sees.

**Sequencing:** start with the two screens an admin operates routinely — KYC review and withdrawal
approval (both already have proven backends, only the UI is new) — then broaden to distributor
search/management and commission oversight (absorbing the original Task 22/23 scope). Purely
archival views (raw wallet ledger, commission cycle logs) stay on Django Admin unless later
decided otherwise.

- [ ] Task 22: Admin Portal — KYC review screen (first slice)
- [ ] Task 23: Admin Portal — withdrawal approval, distributor search/management, commission
  oversight (absorbs the original Task 22/23 scope; broken into 23a/23b/23c sub-slices when
  started, same pattern as Task 16a-16h — kept as one task number, not new top-level numbers, so
  Task 24's existing "deploy pipeline" references elsewhere in `tasks/todo.md`/`CLAUDE.md` don't
  need renumbering)

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
5. ~~Min/max withdrawal amount + withdrawal day~~ — resolved 2026-07-22: GHS 100 minimum, GHS
   10,000 maximum per request, processed Fridays. Seeded in `apps/platform_settings/config.py`;
   admin-editable, not a permanent code decision. Task 16 is now unblocked.
6. ~~Existing Paystack/mNotify accounts, or need to create them~~ — resolved: mNotify confirmed
   2026-07-11 (API key in hand); Paystack confirmed 2026-07-13 (test and live API keys in hand,
   already used successfully in Tasks 10b/10c's registration-fee and starter-pack payments)
7. ~~Confirm `pyenv` Python + Homebrew `mysql`/`redis` actually install cleanly on this Mac~~ —
   resolved: Python (already present, no `pyenv` needed) and Redis installed cleanly; MySQL did
   not, so local dev uses SQLite instead (see `SPEC.md` Local dev environment)
8. Wire up GitHub Actions CI with a real MySQL service container once a CI provider is confirmed
   (Open Question #1) — this is the safety net for the SQLite/MySQL concurrency gap above, needed
   before Phase 4 (Commission Engine) merges anything, not blocking Task 1

Each of these will be asked as a short question right before its blocking task starts, rather than
all at once now.
