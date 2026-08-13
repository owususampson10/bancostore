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
- [x] Task 16: Withdrawal request flow — tax deduction, Paystack payout, admin approval. Scoped via
  `spec-driven-development` (four money-safety design questions with no answer in `SPEC.md` — payout-
  destination capture, `WITHDRAWAL_DAY` semantics, wallet debit timing, the `AUTO_APPROVE_*`
  settings' fate against a hard Boundary — put to the user directly rather than assumed, resolved
  in `docs/decisions/0004-withdrawal-payout-design.md`), built as 8 vertically-sliced sub-tasks
  (16a–16h, see `tasks/todo.md`). Shipped 2026-07-24; see `CLAUDE.md` for the full build/Checkpoint
  F narrative, including the sandbox Paystack account-tier limitation on real Transfers.

**Checkpoint F:** a distributor requests a withdrawal, tax is deducted correctly, admin approves,
and a simulated Paystack payout succeeds. **Passed** 2026-07-24, real browser session, every
financial figure verified against the database at each step.

### Phase 6: Checkout & Orders
- [x] Task 17: Cart + checkout — delivery/pickup, delivery-fee-by-zone, Paystack payment. Built as
  17a–17f (see `tasks/todo.md` for the full sub-task breakdown and build narrative, `CLAUDE.md` for
  the phase-level summary). Delivery zone fee table resolved via ADR-0005 decision 1 (the source
  doc's own worked example: Kumasi GHS 20 / Accra GHS 50 / Other Regions GHS 70). Shipped
  2026-07-25 via PR #24 (17a), #25 (17b), #26 (17c), #27 (17d) — CodeRabbit found 5 real issues on
  17d specifically, all fixed pre-merge. 17e's mobile-responsive browser pass ran 2026-07-26:
  `cart.html`/`checkout.html`/`order_created.html` verified clean at 1440/1024/768/500px real
  browser resize — true 320px wasn't reachable (macOS Chrome's ~500px window-resize floor, and the
  iframe workaround is correctly blocked by Django's own `X-Frame-Options: DENY`, not weakened just
  for a test); code inspection found no fixed-width elements that would break specifically below
  500px — see `tasks/todo.md` Task 17e for the full reasoning.
- [x] Task 18: Order status lifecycle + notifications + admin order management — scoped 2026-07-26
  via `docs/decisions/0006-order-lifecycle-and-admin-management-design.md` (read directly against
  the primary source doc's Section 5.2/5.3, plus four decisions confirmed with the user:
  cancelling/refunding a confirmed order reverses stock *and* PV, not just status; refunds stay
  manual, no real Paystack Refund API integration is built here after all; customer-facing
  self-service cancel is deferred to a follow-up task; the admin order view is a custom
  Stitch-designed `admin_portal` page, not plain Django Admin) → built as 18a-18g in `tasks/todo.md`,
  matching Task 17's own 17a-17f granularity. Shipped 2026-07-26 via PR #33; full suite green
  throughout, 945 passed as of merge.

**Checkpoint G:** both a customer and a distributor complete a purchase; PV is generated only for
the distributor purchase; order status updates correctly. **Passed in full** 2026-07-26 — the
purchase + PV-branching portion passed 2026-07-25 (guest checkout verified live end-to-end,
distributor PV-credit branch verified via `confirm_order_payment`'s own pytest suite); the "order
status updates correctly" portion closed 2026-07-26 with `MNOTIFY_API_KEY` temporarily blanked
(explicit user sign-off) to exercise the real fake-sender notification path live through the admin
UI.

### Phase 7: Cooling-Off Refund
- [x] Task 19: 7-day cooling-off refund — processing fee, PV reversal, commission reversal. Built as
  19a-19c per `docs/decisions/0007-cooling-off-refund-design.md` (the 7-day window anchors to
  `starter_pack_confirmed_at`, not registration; `BinaryTreeEdge` placement is never removed, the
  distributor is soft-deactivated instead; only the sponsor's one-time direct referral bonus is
  reversed, not any Binary/Matching bonus; a sponsor-wallet shortfall is logged and the refund
  proceeds regardless; the refund is credited to the distributor's own wallet). CodeRabbit caught
  two real, previously-unnoticed defects across the PR stack: `reverse_ancestor_pv` (19a) skipped a
  root distributor's own personal PV reversal (fixed, also affected Task 18b's order-cancellation
  path); and the refund credited at cancellation was unclaimable, since the same call also
  deactivates the account and Django blocks login entirely for `is_active=False` — fixed by letting
  a cooling-off-cancelled distributor log in specifically to claim it, routed to withdrawal-only
  views. See `CLAUDE.md` for the full build narrative. Shipped 2026-07-27 via PR #35 (19a), #36
  (19b), #37 (19c), each stacked on the previous and merged into `main` in order; full suite green
  throughout, 986 passed, 1 skipped as of merge.

### Phase 8: Distributor Dashboard (real-time)
- [x] Task 25: My Orders — self-service order history. Added 2026-07-27, found by reading Section
  6.4 of the primary source doc directly while scoping Task 20 below (`source-driven-development`)
  — no task anywhere in this plan built a self-service order-history page for either a customer or
  a distributor. Numbered 25 (not slotted in as 20) since Task 20-24 are all already real,
  shipped-code-referenced numbers (20/21 here, 22-24 in Phase 9/Deploy) — none of them move; this
  task just builds *before* Task 20 despite the higher number. See `tasks/todo.md` for the full
  breakdown and the reasoning behind the non-sequential numbering. Shipped 2026-07-28 via PR #38,
  merged to `main`; full suite green throughout (1009 passed, 1 skipped), real MySQL CI green,
  CodeRabbit clean on the final commit.
- [x] Task 20: Dashboard core stats (wallet, earnings, team size, leg PV, rank, referral link).
  Broken into vertical slices 20a-20d 2026-07-28 (`planning-and-task-breakdown`) — first Channels
  consumer this codebase has ever built (Channels/Redis configured since Task 1-3, but
  `bancostore/asgi.py`'s websocket router has always been empty), plus a small, deliberate touch to
  Task 10a's registration form (a real "recruitment link" per Section 6.1 needs `?ref=` prefill
  support, not just a bare IR ID to type in). See `tasks/todo.md` for the full 20a-20d breakdown.
  **20a+20b done** (PR #39, merged). **20c done** (PR #40, merged 2026-07-28) — real
  Stitch-designed frontend, verified live at 1440/1024/768/500px. **20d done** (live wallet
  updates via Channels) — a `doubt-driven-development` cycle (single-model + two independent
  external reviews, ChatGPT and Gemini) ran before any consumer code was written and materially
  changed the design; see `docs/decisions/0008-wallet-live-updates-channels-design.md`. Verified
  live in a real browser: crediting a wallet from a separate shell process updated the dashboard's
  balance with no page refresh. Also found and fixed, via that same real-browser check, an
  unrelated pre-existing gap — `daphne` doesn't auto-serve static files in DEBUG mode the way
  `runserver` does — documented in `CLAUDE.md`.
- [x] Task 21: Binary tree visual view, earnings history, carry-forward tracker, notification bell.
  Re-scoped 2026-07-28 via `source-driven-development` + `planning-and-task-breakdown` — the
  existing entry's acceptance criteria were incomplete (earnings history and the carry-forward
  tracker had none at all). Sliced into 21a (tree view), 21b (carry-forward tracker), 21c (confirm
  earnings history is already satisfied by Task 15/20c — verification only), 21d (notification
  bell — the biggest piece, a second Channels consumer needing its own `doubt-driven-development`
  pass, same as Task 20d). See `tasks/todo.md` for the full breakdown.
  - [x] **21a done (PR #44, merged 2026-07-28):** `apps/binary_tree/services.py::get_downline_tree`,
    a flat 3-query downline fetch (never a recursive DB walk) assembled into an in-memory tree, plus
    the IDOR-safe `binary_tree_view` and its templates. CodeRabbit caught two real recursive-Python
    functions (tree assembly and node counting) that could theoretically hit Python's recursion
    limit on a pathologically deep single-line downline — both converted to iterative before merge.
  - [x] **21b done (PR #47, merged 2026-07-29):** `apps/pv_ledger/services.py::get_carry_forward_summary`,
    a new dashboard stat card showing PV carried forward on the distributor's strong leg (per Section
    6.5's own wording) plus its nearest expiry date. Reuses the exact same weak/strong leg comparison
    the real Binary Bonus cycle already uses, so the display can never drift from the real payout
    logic. A `doubt-driven-development` pass caught and fixed a real double-read of a live
    admin-editable constance setting before it shipped.
  - [x] **21c done (2026-07-29, verification only, no code changed):** confirmed live against a
    distributor seeded with all three bonus types that Section 6.3's three requirements (date+time,
    distinguishable bonus type, amount credited) were already fully satisfied by Task 15's
    `earnings_history` page — no gap found.
  - [x] **21d-i, 21d-ii, 21d-iii done (PRs #48/#49/#50, merged 2026-07-29/30):** the notification
    bell's backend is complete — `Notification` model, the second real Channels consumer
    (`NotificationConsumer`), `send_notification` (on_commit-deferred, doubly-try/excepted per a
    doubt-driven-development pass that caught two Critical bugs in the original design before any
    code was written), all 6 of Section 6.6's event types wired (5 immediate triggers plus the
    PV-expiry scheduled check, which reuses Task 21b's `get_carry_forward_summary` directly so the
    two can never disagree).
  - [x] **21d-iv done (2026-07-30), closing out Task 21:** the bell UI itself, built from 4 fetched
    Stitch screens (desktop populated/empty, mobile populated/empty), reconciled against real scope
    (dropped fabricated dashboard chrome, illustrations, a mobile-only settings-gear icon/"Refresh
    Portal" button/category filter chips — none of which are real features). Live-browser
    verification caught and fixed three real bugs no test suite would have: a multi-line Django
    `{# #}` comment rendering as literal text (the same recurring footgun from Task 18f, twice in
    this task alone), `backdrop-blur-sm` on the header silently trapping the mobile full-screen
    overlay inside the header's own 64px height (a CSS `backdrop-filter` establishes a containing
    block for `position: fixed`, just like `transform`), and a two-layer live-badge failure — a
    `channels-redis`/`redis-py` 8.x version incompatibility crashing the WebSocket connection
    (fixed with a user-approved `redis<5` pin, confirmed to also silently affect the already-shipped
    Task 20d wallet-balance push) plus a stale cached DOM reference the header JS held past htmx's
    own oob-swap replacing that node. A code-review pass caught one more real, 100%-reproducible bug
    pre-merge: the cooling-off-cancelled-distributor redirect decorator, applied to the new
    htmx-loaded dropdown view, swapped an entire page into the small dropdown panel — fixed by
    removing it from all 4 new views (viewing notifications isn't earning-related, matching the
    withdrawal-flow views' existing exemption). A parallel security pass found zero exploitable
    issues. See `tasks/todo.md` Task 21d-iv for the full breakdown.

**Checkpoint H:** dashboard values update live (no page refresh) when a commission is credited in
another session — proves Django Channels real-time wiring works.

### Phase 9: Admin Portal — custom Stitch-designed dashboard (redefined 2026-07-23) — COMPLETE
**Scope change from Django Admin to a full custom dashboard, decided 2026-07-23:** the admin user
is not software-literate — every screen an admin touches routinely needs to feel like part of the
Bancostore application, not Django's generic admin panel. **Shipped in full the same day** (PRs
#12, #14, #15, #16, #17), with two follow-up polish passes (#23: mobile table/typography fixes
across 6 pages; #34: shared flash-message component). **Correction 2026-07-28: this checklist had
gone stale — Task 22 and 23 were both fully built, tested, and merged back on 2026-07-23, but
never marked done here.** Verified against the real `apps/admin_portal/` codebase (`urls.py`,
`views.py`) and its full test suite (`tests/feature/admin_portal/`: KYC review, withdrawal review,
distributor directory, commission oversight, order management, dashboard — all present and
passing) before correcting this doc, not assumed from memory.

**Outstanding, not yet done:** `SPEC.md`'s "Admin panel | Django Admin (customized)" tech-stack
line still doesn't match this shipped reality (a custom Stitch-designed portal fronting Django
Admin's service functions, with Django Admin as a technical fallback for archival views only) —
flagged to the user 2026-07-28, held pending explicit confirmation before editing `SPEC.md` per
this project's own convention.

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

**Sequencing:** started with the two screens an admin operates routinely — KYC review and
withdrawal approval (both already had proven backends, only the UI was new) — then broadened to
distributor search/management and commission oversight (absorbing the original Task 22/23 scope).
Purely archival views (raw wallet ledger, commission cycle logs) stay on Django Admin.

- [x] Task 22: Admin Portal — KYC review screen (first slice) — PR #12
- [x] Task 23: Admin Portal — withdrawal approval, distributor search/management, commission
  oversight (absorbed the original Task 22/23 scope) — withdrawal review PR #14, distributor
  directory + profile PR #15, real-time search/filter + CSV export PR #16, commission oversight +
  cycle detail PR #17, all merged 2026-07-23; polish follow-ups PR #23 (mobile table/typography)
  and PR #34 (shared flash-message component). Full test coverage in
  `tests/feature/admin_portal/`.

**Checkpoint I (final MVP checkpoint):** the full Section 14 distributor journey works end to end
through the UI. All pytest tests pass, `black`/`ruff` clean. Verify against `SPEC.md` Success
Criteria.

- [x] Task 26: Admin Portal — Catalog Management (categories + products). Not in the original plan
  — found by auditing which already-shipped backend features (Task 7's catalog models) still had
  no dedicated `admin_portal` frontend, the same audit that turned up Task 27. Built from 5 fetched
  Stitch screens; a dynamically-sized inline product-image formset with a
  `normalize_primary_image()` helper guaranteeing exactly one primary image per product. Closed out
  2026-07-31 — PR #53. See `tasks/todo.md` for the full breakdown.

- [x] Task 27: Admin Dashboard — replaces the Task 22 placeholder now that every other
  `admin_portal` section had actually shipped. Built from a fetched Stitch screen, reconciled
  against real scope (dropped a global search bar, notification bell, and other mockup chrome with
  no corresponding real feature); every number on the page is a real query (pending KYC/withdrawals,
  orders awaiting action, low-stock products, a rolling 7-day business snapshot). Closed out
  2026-07-31 — PR #54. See `tasks/todo.md` for the full breakdown.

- [x] Task 28: Platform Settings admin screen — replaces raw Django Admin as the primary path for
  all 76 `django-constance` business-rule settings (10 fieldset groups), the last remaining
  admin-facing surface not yet matching the rest of `admin_portal`'s look. Closed out 2026-07-31 —
  PR #55. See `tasks/todo.md` for the full breakdown.

- [x] Task 29: Storefront About & Contact pages — replaces the 6 dead "Coming soon" links in
  `templates/base_store.html` with real pages; content grounded in `SPEC.md`'s Objective section
  and the already-seeded-but-unused `CONTACT_*`/`PHYSICAL_ADDRESS` constance settings, never
  fabricated. Closed out 2026-07-31. See `tasks/todo.md` for the full breakdown.

- [x] Task 30: Fix tracked Known Issues (decorative constance settings, `SESSION_ENGINE` DB
  fallback, CI hygiene) — 6 vertical slices (30a-30f), per explicit user go-ahead 2026-07-31. Two
  scope decisions were user-confirmed rather than assumed: `PASSWORD_RESET_EXPIRY_MINUTES` and
  `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED` get honest "not yet enforced" documentation instead of new
  security-token/distributor-Google-login feature builds. Closed out 2026-07-31 — PR #57. See
  `tasks/todo.md` for the full breakdown.

- [x] Task 31: Storefront home page — Editorial Variant layout rebuild, per direct user request.
  Rebuilds the Hero, Shop By Category, How It Works, and Featured Products sections of
  `templates/catalog/home.html` to match the "Storefront Home - Editorial Variant" Stitch screen;
  the other 5 sections were already equivalent and stayed untouched. All real
  `Category`/`Product` data wiring preserved. Two real responsive overflow bugs (hero headline vs.
  its split-layout column at 768px and again at exactly 1024px) found and fixed via live-browser
  testing. Closed out 2026-08-04. See `tasks/todo.md` for the full breakdown.
  - **Follow-up (2026-08-04/05), per direct user feedback after live-testing the shipped page:**
    hero headline/subtext now centers on mobile/tablet (reverts to left-aligned at `lg:`); the
    bento grid's second tile also gets a wide `md:col-span-2` span (matching the real Stitch
    mockup, not just the first tile); bento text moved from centered to bottom-left on every tile;
    "View All Categories" moved outside the grid into its own link, with the in-grid "View All
    Products" tile now an `lg:hidden` fallback; the timeline's "square instead of circle" bug
    root-caused to this project's `rounded-full` token being redefined to `0.75rem` (for pill
    buttons, not true circles) — fixed with `rounded-[50%]` on the actual circular badges, plus a
    `motion-safe:animate-pulse` on the first timeline circle; the featured-products carousel's
    Previous/Next buttons now show at every breakpoint, not desktop-only. Shipped in PR #60
    alongside Task 32 below.

- [x] Task 32: Admin portal fixes — category-image dropzone, logout redirect, native-admin links.
  Not in the original plan — found via direct user testing/reports in the same session as Task 31's
  follow-up. Replaced the Category admin form's raw, unstyled Django `ClearableFileInput` (literal
  "Currently: ... / Clear / Choose file No file chosen" text) with a modern Alpine-driven
  dropzone/preview UI matching the product-image dropzone's existing visual language
  (`CategoryImageWidget`, a small `ClearableFileInput` subclass with its own template — required a
  small global `FORM_RENDERER = TemplatesSetting` + `django.forms` in `INSTALLED_APPS` fix, since
  Django's default form renderer can't see this project's real `templates/` directory). Two real
  bugs found live and fixed: a `ValueError` crash editing any category with no image
  (`{{ category.image.url }}` on an empty `ImageField` — Django's `|default` filter can't catch
  exceptions, only falsy values), and a click-delegation bug where the dropzone's `<label>` wrapped
  both the file input and Django's "clear" checkbox — a label wrapping two labelable controls
  delegates its click to the first one, so clicking after deleting an image silently did nothing
  (fixed with an explicit `for=`/`id` association). Also fixed: admin logout was posting to
  `admin:logout` (Django's own raw internal logout view), redirecting to the native unstyled
  `/admin/login/` page instead of this project's branded one — switched to `account_logout`
  (already the storefront's own convention) with a redirect back to the branded admin login. Two
  more leftover native-admin links found via a follow-up audit: the admin login page's logo linked
  to itself (no way to reach the public storefront from that screen) — now links to `catalog:home`;
  the 2FA setup-complete screen's "Continue to Admin Panel" button linked to `admin:index` (raw
  native admin dashboard) — now links to `admin_portal:dashboard`. A CodeRabbit review on PR #60
  caught one more real, Major bug: the category dropzone's delete button was only revealed via
  `group-hover`, and touch devices have no hover state at all, so an admin on a phone/tablet could
  never discover or reach it — the same latent bug already existed in the pre-existing
  product-image dropzone this was modeled on; fixed both the same way
  (`opacity-100 md:opacity-0 md:group-hover:opacity-100`). Shipped 2026-08-05 via PR #60 (5 commits
  total, including one follow-up addressing CodeRabbit's findings); full CI green (lint, real-MySQL
  test, CodeRabbit) before merge. See `tasks/todo.md` for the full breakdown.

- [x] Task 33: Distributor Team page — flat roster of the personally-recruited downline. Scoped and
  built 2026-08-10. The "Team" sidebar entry had existed as a disabled placeholder since Task
  15/21 ("still a future task"); this gives it a real, user-confirmed scope. Deliberately NOT the
  same data as the existing Binary Tree page (Task 21a, `apps.binary_tree`'s placement/spillover
  structure) — this is the *sponsor* chain (`Distributor.sponsor`, the recruitment lineage
  Matching Bonus already walks), which the codebase already documents as diverging from placement
  under spillover. Columns: full name, IR ID, rank, KYC status, date joined — all real
  `Distributor`/`User` fields, no fabricated data (no Personal PV column — user-confirmed
  2026-08-10, kept to the fields free from one query). Full downline depth (not just direct
  recruits) via the same bulk-per-level BFS pattern `apps.commissions.services` already used for
  Matching Bonus — actually extracted into a shared, independently-tested
  `walk_sponsor_chain_downline_ids` function (a behavior-preserving refactor, verified against the
  existing Matching Bonus test suite before and after) rather than left as "reused, not
  duplicated" in name only. Search/sort on the roster explicitly deferred as a fast-follow, not
  built in this first slice. See `tasks/todo.md` for the full build breakdown.

### Phase 10: Deployment — complete 2026-08-10
- [x] Task 24: Deploy to Hostinger VPS (production) — `https://bancostore.com` is live. Broken into
  vertical sub-tasks 24a-24h, each its own production action confirmed with the user before
  running (see `tasks/todo.md` for the full breakdown, real bugs found and fixed along the way,
  and 24h's full live smoke-test story).

**Checkpoint J (go-live) — reached 2026-08-10:** Bancostore is reachable over HTTPS at
`bancostore.com`, running on the Hostinger VPS against real MySQL, with Celery/Celery Beat/Daphne
kept alive by Supervisor and verified to survive both a process crash and a full server reboot. A
complete distributor journey (registration → real Paystack payment → tree placement → Direct
Referral Bonus → real Didit KYC → admin approval → IR ID → withdrawal request/approval/tax) was
verified live against the production database before being cleaned up, leaving zero distributors
ahead of real launch.

### Phase 11: SEO — new scope, not in `SPEC.md` (like Tasks 29/34/35), started 2026-08-11
The app is live at `bancostore.com` (Checkpoint J) with almost no SEO infrastructure: an audit
found no `robots.txt`, no sitemap, no canonical/Open Graph/Twitter Card tags, no JSON-LD structured
data anywhere, and the home page (`templates/catalog/home.html`) doesn't even override the generic
site-wide `title`/`meta_description` blocks. `Product`/`Category` already have unique slugs and
slug-based URLs, so no schema change is needed to build on top of them.
- [x] Task 37: SEO foundations, split into 4 vertical slices (37a-37d)
  - [x] 37a: Domain-aware meta framework — canonical URL + Open Graph + Twitter Card tags added to
    `templates/base_store.html`, using `request.scheme`/`request.get_host` (proxy-aware, since
    `SECURE_PROXY_SSL_HEADER` is already configured for the production Nginx setup — no new
    `SITE_URL` setting or `django.contrib.sites` wiring needed). Fixes the home page's missing
    `title`/`meta_description` override. See `tasks/todo.md` Task 37 for the full build/verify
    detail.
  - [x] 37b: `robots.txt` + XML sitemap via `django.contrib.sitemaps`, covering products and
    static/legal pages (no per-category URL exists to sitemap — `product_list` filters by
    `?category=` query param, and its own canonical tag always points to the bare path); precise
    `robots.txt` disallow list, not a blanket block (distributor registration/login stay
    crawlable — a real acquisition funnel page — only the authenticated dashboard sub-paths are
    blocked). See `tasks/todo.md` Task 37 for the full build/verify detail.
  - [x] 37c: JSON-LD structured data — `Organization`/`WebSite` sitewide (with a real
    `SearchAction` for Google's sitelinks search box, and `sameAs` sourced from Task 35's admin-
    managed social links), `Product` schema on `product_detail` (real price/stock/image, no
    fabricated `image` when a product has none). See `tasks/todo.md` Task 37 for the full
    build/verify detail.
  - [x] 37d: Page speed / Core Web Vitals audit (real Lighthouse via `npx`, Deep mode) — diagnostic
    pass only, no fixes applied. Top findings: a 1.1MB un-subsetted Material Symbols font (>60% of
    home page weight), ~9 home-page images still served live from Google's Stitch-generation host
    (`lh3.googleusercontent.com`) instead of self-hosted, no text compression on Nginx, HTTP/1.1.
    See `tasks/todo.md` Task 37 for full detail. Fixes deliberately not bundled in — flagged to the
    user as a candidate follow-up task.

### Phase 12: Performance fixes from the Task 37d audit — Task 38, 2026-08-11
- [x] Task 38: Self-host the remaining Stitch-generated images still loading live from
  `lh3.googleusercontent.com` — one of Task 37d's findings, acted on the same session rather than
  deferred, since a comment already in `templates/catalog/home.html` (2026-08-07) showed one of
  these exact links had already expired and broken production once. See `tasks/todo.md` Task 38
  for the full detail. The font-subsetting and Nginx-level findings (text compression, HTTP/2) from
  the same audit are still open, not silently dropped — they need either a new build-time
  dependency or production server config changes, both Boundaries requiring the user's sign-off
  first.

**Checkpoint K — reached 2026-08-11:** `robots.txt`/sitemap reachable and valid (live-verified
against `runserver`), every audited page has a real canonical/OG/Twitter/title/description
(live-verified, including the previously-missing home page), JSON-LD parses as valid JSON with
real database content on both a static and a dynamic page (live-verified — Google's Rich Results
Test itself wasn't run, since it requires submitting a real production URL to an external Google
tool, not attempted here), page-speed audit complete with findings reported to the user, Task 38
acted on the highest-risk one. **Test status, stated precisely rather than as one blanket "full
suite green" claim (a CodeRabbit finding on PR #70 — the original wording here conflated a
targeted-suite pass with a full-suite one two paragraphs below it):** every targeted suite for
each individual slice was green at the time that slice was built (see `tasks/todo.md` Task 37/38
for each slice's own targeted-suite numbers); one full local `pytest -q` run after 37a+37b was
green except the one pre-existing, unrelated Task 36 failure (`git stash`-confirmed to already
exist on `main`); the real-MySQL GitHub Actions CI run on PR #70 itself is the authoritative
check, since SQLite's locking hides concurrency bugs CI's MySQL container would catch (see
`SPEC.md` Testing Strategy) — its result is recorded in `tasks/todo.md` once known, not assumed
green here in advance.

## Phase 2 (SPEC_PHASE2.md) — Tasks 39-48

Not part of the original MVP plan above — scoped 2026-08-12 from `SPEC_PHASE2.md`, which itself
grounds every feature directly in the primary source doc's Sections 10.2/10.3, 11, 12.4–12.6,
13.5–13.8/13.10–13.12, and 4.2 (read via `python-docx`, not paraphrased). Ten features, numbered
39-48 to match `SPEC_PHASE2.md`'s own Feature 1-10 numbering exactly, so either document can be
read against the other with no translation. Grouped into four sub-phases by risk/coupling profile,
not strict build priority — simple, independent customer-facing features first, money-adjacent
merchandising features second (once first-slice conventions are re-established), then admin-facing
reporting/compliance (the largest, most interdependent group), then the broadest cross-cutting
feature last (Notification Template Editor touches five apps). **This ordering is a judgment call,
not a fixed sequence — re-order freely if a different feature is more urgent.**

Every task below follows this project's own vertical-slice convention (db + backend + frontend
together, tested and live-browser-verified before the next task starts) and is broken into lettered
sub-tasks wherever the full feature would otherwise exceed the ~5-file/single-session sizing this
plan has held every prior task to. Full acceptance criteria, verification steps, dependencies, and
file lists for every task/sub-task are in `tasks/todo.md`, not duplicated here — this file lists
scope and rationale only, per its own established format above.

### Phase 13: Phase 2 — Customer Account Features
- [x] Task 39: Wishlist — save/remove products, account-page list, account-backed not session-backed
- [x] Task 40: Saved / multiple delivery addresses
  - [x] 40a: `Address` model + account CRUD (save, edit, delete, set default)
  - [x] 40b: Checkout gains "choose a saved address or enter a new one," still snapshots onto
    `Order` exactly as today — editing a saved address later must never change an already-placed
    order (a real regression test, not just a manual check)
- [x] Task 41: Product Reviews
  - [x] 41a: Review submission — gated to customers with a `delivered` order for that specific
    product, one review per customer per product (edit-in-place on resubmission)
  - [x] 41b: Admin moderation (approve/delete, `admin_portal`) + public display (approved reviews +
    average rating on the product page) + the 13.10 "Product Review Approval" admin toggle

### Checkpoint L — after Tasks 39-41
- [x] Test status, stated precisely rather than as one blanket claim: every targeted suite (catalog,
  accounts, admin_portal, orders) was green at the time each task's slice was built; multiple full
  local `pytest -q` runs were green except the one pre-existing, unrelated Task 36 regression
  (`test_page_loads_the_shared_js_bundle_so_the_sidebar_can_actually_collapse`, confirmed pre-
  existing before this work started); GitHub Actions CI on PR #73 (lint + real-MySQL test) both
  passed
- [x] Live-browser verified: wishlist survives a logout/login cycle; a saved address correctly
  pre-fills and snapshots at checkout; a review only appears publicly after admin approval
- [ ] Review with the user before proceeding to Phase 14

### Phase 14: Phase 2 — Merchandising & Promotions (money-adjacent — elevated rigor)
- [ ] Task 42: Promotional Banners — admin upload (image, link target, start/end date), home page
  shows only currently-active banners with no admin action needed on expiry
- [ ] Task 43: Discount Codes — **`doubt-driven-development` pass before 43b, matching every other
  money-adjacent feature in this codebase**
  - [ ] 43a: `DiscountCode` model + admin CRUD + the 13.11 settings (Discount Codes toggle, Maximum
    Discount Per Order, Discount Applicable To)
  - [ ] 43b: Checkout redemption — snapshotted discount amount on `Order` (never re-derived from a
    possibly-since-expired code later), concurrency-safe usage-cap decrement (same atomic-counter
    discipline as `Product` stock, not a naive read-then-write) — a real concurrent-redemption test
  - [ ] 43c: Audience restriction wired to real customer/distributor role groups (`is_distributor`
    etc.), not just `Order.pv_earned`
- [ ] Task 44: Backorders — **`doubt-driven-development` pass before 44b**, since it changes
  `confirm_order_payment`'s existing out-of-stock behavior (Task 17d) — a real interaction with
  already-shipped money-adjacent code, not a green-field addition
  - [ ] 44a: Per-`Product` backorder fields + storefront Add-to-Cart/"Ships in N days" messaging +
    the 13.6/13.10 settings (Backorders On/Off, Backorder Message, Out of Stock Behaviour)
  - [ ] 44b: Checkout/order-confirmation integration — a backorder-enabled item must not hit Task
    17d's existing cancel-and-refund path; a non-backorder item's existing behavior must be
    unchanged (regression test against Task 7/17's existing suite)

### Checkpoint M — after Tasks 42-44
- [ ] Full suite green including new elevated-rigor coverage for Task 43/44 (concurrency,
  expiry/audience edge cases, the Task 17d interaction), CI green on real MySQL
- [ ] Live-browser verified: a valid code reduces checkout total correctly; an expired/exhausted/
  audience-mismatched code is rejected with a clear message, never a 500; a backordered product
  can be ordered and does not auto-cancel
- [ ] Review with the user before proceeding to Phase 15

### Phase 15: Phase 2 — Admin Reporting & Compliance
- [ ] Task 45: Shared CSV/PDF export utility (`export_as_csv`/`export_as_pdf`, matching
  `bancostore/concurrency.py`'s existing shared-utility precedent) — built first since Tasks 46-47
  depend on it; PDF path reuses the already-proven `WeasyPrint` pipeline from Task 18e (same
  `project_weasyprint_pango_blocked_locally` local-verification caveat applies, CI-verified instead)
- [ ] Task 46: Sales & Revenue Reporting
  - [ ] 46a: Reporting-at-scale architecture decision — pre-aggregated rollup tables (matching the
    PV ledger's own event-driven-aggregate precedent) vs. a scheduled Celery report-cache job vs. a
    bounded live-query lookback window. Written up as a new ADR (`docs/decisions/0010-...md`,
    matching Tasks 4/9/12/13/16/17/18/19/20/35's own precedent) before any report code — this is a
    real architectural decision per `SPEC.md`'s explicit hundreds-of-thousands-of-users scale target,
    not a detail to improvise mid-build
  - [ ] 46b: Revenue (daily/weekly/monthly), order-per-status, and delivery-by-zone reports, wired
    to Task 45's export utility
  - [ ] 46c: Best-selling products + new-vs-returning customer reports, wired to Task 45
- [ ] Task 47: Compliance Dashboard + Financial Overview — **`doubt-driven-development` pass before
  47a**, the escrow ledger is new money-adjacent code
  - [ ] 47a: Escrow reserve ledger — atomic `F()`-based running balance credited at order
    confirmation (matching `Wallet.balance`'s own convention), 13.12's admin-editable Escrow
    Reserve Percentage (not hardcoded to 5%)
  - [ ] 47b: Retail/distributor ratio (`Order.pv_earned > 0` as the existing real signal) + the
    13.12 Retail PV Minimum threshold + Compliance Alert Email (reuses the existing Gmail SMTP path)
  - [ ] 47c: Financial Overview dashboard row (four numbers, all already-real underlying data —
    smallest-scoped sub-task in this phase, matching Task 27's own dashboard-card pattern)
  - [ ] 47d: Audit log, part 1 — add `HistoricalRecords()` to the sensitive models that are missing
    it (Product/Category from Task 26, Platform Settings from Task 28, KYC approve/reject) — a
    decision on which models make the cut is a Task-kickoff question, not decided in this plan
  - [ ] 47e: Audit log, part 2 — one real `admin_portal` screen querying across every
    `HistoricalRecords()`-tracked model (actor, timestamp, what changed), plus the 13.12 Audit Log
    Retention Period wired as a real scheduled Celery cleanup, not decorative

### Checkpoint N — after Tasks 45-47
- [ ] Full suite green including new elevated-rigor coverage for Task 47a (escrow), CI green on
  real MySQL
- [ ] Live-browser verified: every report/export figure cross-checked against the database directly
  (not just "the page/file renders"); escrow balance increases by exactly the configured percentage
  of confirmed product revenue, checked against the database; the compliance alert email actually
  fires when seeded data drops the ratio below threshold, and does not fire above it
- [ ] Review with the user before proceeding to Phase 16

### Phase 16: Phase 2 — Notifications & Settings Completion
- [ ] Task 48: Notification Template Editor + Provider-Choice Settings — the broadest task in Phase
  2, touching `apps/notifications`, `apps/accounts`, `apps/distributors`, `apps/withdrawal`,
  `apps/commissions`; mNotify/Gmail SMTP stay the only wired providers, per direct user confirmation
  (a `security-and-hardening` pass on template rendering is mandatory before 48a ships, given
  admin-entered text rendering into SMS/email/HTML is a real injection surface)
  - [ ] 48a: `NotificationTemplate` model (name/subject/body/placeholder variables) + admin CRUD in
    `admin_portal`, with server-side escaping matching this codebase's established "constrain
    server-side, never raw interpolation" rule (Task 17/37's Alpine `x-data`/JSON-LD precedent)
  - [ ] 48b: Migrate the highest-traffic send-sites to render from a template — OTP codes,
    withdrawal status (approved/rejected/paid/reversed), KYC decision
  - [ ] 48c: Migrate the remaining send-sites — binary/matching/direct-referral bonus credited,
    downline joined, PV-expiry warning, order status updates (Task 21d's 6 event types plus every
    remaining auth/transactional message since Task 4)
  - [ ] 48d: SMS Provider / Email Provider admin-editable choice fields (visibly labeled as the one
    real wired option, matching the Currency-lock precedent from Task 36g/h — never a functional
    no-op picker) + Sender Name / Sender Email Address moved from `.env`/hardcoded into real
    constance settings

### Checkpoint O — Phase 2 complete
- [ ] Every one of `SPEC_PHASE2.md`'s ten Success Criteria sections met
- [ ] Full suite green, CI green on real MySQL, every feature live-browser-verified
- [ ] `SPEC.md` cross-references confirmed accurate (already pointing at `SPEC_PHASE2.md` as of
  2026-08-12); `CLAUDE.md` Project State updated to record Phase 2's completion
- [ ] Review with the user — Phase 2 sign-off

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
| Discount Codes (Task 43b) / Backorders (Task 44b) touch already-shipped, locked/idempotent checkout code (Task 17d/18b) | High — a regression here risks double-charging or breaking existing checkout | `doubt-driven-development` pass before each, per `SPEC_PHASE2.md`'s own Boundaries; regression tests against the existing Task 7/17/18 suites, not just new tests |
| Reporting (Task 46) run as naive live aggregate queries at `SPEC.md`'s stated hundreds-of-thousands-of-users scale | Medium — slow/costly dashboard once real volume exists | Task 46a is a dedicated architecture-decision sub-task (ADR) before any report code, not improvised mid-build |
| Notification Template Editor (Task 48a) renders admin-entered text into SMS/email/HTML | Medium — a real injection surface if unescaped | `security-and-hardening` pass mandatory before 48a ships; server-side constrain, never raw interpolation, matching Task 17/37's existing precedent |

## Open Questions (carried from `SPEC.md`, mapped to blocking tasks)

1. ~~CI provider (GitHub Actions assumed)~~ — resolved: **GitHub Actions**, running `pytest`
   against real MySQL and `black`/`ruff` on every push since early in the project; hardened further
   in Task 30f (explicit `permissions:` block, pinned action SHAs, `pip-audit`)
2. ~~SMS provider, Arkesel or Hubtel~~ — resolved 2026-07-11: **mNotify**, API key in hand
3. ~~Email provider, Mailgun or Gmail SMTP~~ — resolved 2026-07-10: **Gmail SMTP**, verified with
   a real send (Task 4); revisit for Mailgun before real production volume
4. ~~Delivery zone fee table + free-delivery threshold~~ — resolved 2026-07-24: Kumasi GHS 20,
   Accra GHS 50, other regions GHS 70 (the source doc's own Section 5.1 worked example),
   free-delivery threshold GHS 500, pickup always free. Seeded as admin-editable
   `django-constance` settings — see `docs/decisions/0005-checkout-cart-design.md`.
5. ~~Min/max withdrawal amount + withdrawal day~~ — resolved 2026-07-22: GHS 100 minimum, GHS
   10,000 maximum per request, processed Fridays. Seeded in `apps/platform_settings/config.py`;
   admin-editable, not a permanent code decision. Task 16 is now unblocked.
6. ~~Existing Paystack/mNotify accounts, or need to create them~~ — resolved: mNotify confirmed
   2026-07-11 (API key in hand); Paystack confirmed 2026-07-13 (test and live API keys in hand,
   already used successfully in Tasks 10b/10c's registration-fee and starter-pack payments)
7. ~~Confirm `pyenv` Python + Homebrew `mysql`/`redis` actually install cleanly on this Mac~~ —
   resolved: Python (already present, no `pyenv` needed) and Redis installed cleanly; MySQL did
   not, so local dev uses SQLite instead (see `SPEC.md` Local dev environment)
8. ~~Wire up GitHub Actions CI with a real MySQL service container~~ — resolved: running since
   early in the project, the safety net for the SQLite/MySQL concurrency gap above; every
   commission/wallet/PV-ledger PR since Task 4 has gone through it

Each of these will be asked as a short question right before its blocking task starts, rather than
all at once now.

## Open Questions — Phase 2 (carried from `SPEC_PHASE2.md`, mapped to blocking tasks)

Resolved via direct user confirmation before `SPEC_PHASE2.md` was written (not re-litigated here):
scope is all ten features now; the escrow tracker (Task 47a) is internal-ledger-only, no real GCB
Bank integration; SMS/Email provider switching (Task 48d) is an admin-editable choice field only,
mNotify/Gmail stay the only wired providers; Wishlist (Task 39) and saved addresses (Task 40) are
in scope.

Resolved 2026-08-13, directly by the user, right before Task 43 started:
1. **Discount code usage limit (blocked Task 43a/43b):** the source doc's own Section 11.1 only
   ever describes a global redemption cap ("this code can only be used 50 times total") — no
   per-customer limit is mentioned there at all. Confirmed with the user as **two independent
   settings on `DiscountCode`**, matching how Shopify/WooCommerce/Stripe handle this in real
   e-commerce systems: a global `max_uses` total, plus a separate `limit_one_per_customer` on/off
   toggle (default on) layered on top. The per-customer toggle is a deliberate addition beyond the
   literal source doc text, not a misreading of it — noted here so the divergence is traceable.

Still open, each to be asked right before its blocking task starts:
2. **`MAX_PRODUCT_IMAGES` (blocks Task 44a if bundled, otherwise standalone):** stay at 5 (the
   current shipped MVP value, Task 26) or move to the source doc's 8? Not silently changed either
   way — it's a live-shipped value, not a fresh decision.
3. **Reporting-at-scale design (blocks Task 46b/46c):** resolved by Task 46a's own ADR, not asked
   separately — see that sub-task.
4. **Which models get `HistoricalRecords()` for the audit log, and whether it needs its own
   dedicated event-stream model instead of/alongside `simple_history` (blocks Task 47d/47e):** a
   real design call, not a detail.
5. **One review per customer per product vs. one per order (blocks Task 41a):** changes the
   schema's uniqueness constraint — the source doc doesn't say, so this needs a direct answer
   before 41a's migration is written, not an assumption baked into it.
