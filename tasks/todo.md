# Task List: Bancostore MVP

Detailed, checkable tasks. See `tasks/plan.md` for phases, checkpoints, and architecture
decisions. Each task = one vertical slice (db + backend + frontend), tested both sides, verified
working before starting the next one.

**Stack note:** tasks below use Django/Python terms (Django Admin instead of Filament, HTMX views
instead of Livewire components, Celery tasks instead of Jobs, pytest instead of Pest) — see
`SPEC.md` Tech Stack for why and for the full Laravel→Django mapping.

---

## Phase 0: Foundation

### Task 1: Scaffold the Django 5 project and install the full stack

**Description:** Create the Django 5 project with the exact stack from `SPEC.md` Tech Stack:
Django Templates + HTMX, Alpine.js, Tailwind v4, Django Admin, django-allauth, django-two-factor-auth,
django-constance, django-simple-history, WeasyPrint, Pillow, Celery + Celery Beat + Flower, Django
Channels, pytest + pytest-django, Black/isort/Ruff. Confirm it runs locally on this Mac using the
Python already installed (no `pyenv` compile needed), with Redis via Homebrew (already installed).
**MySQL does not install locally** — Homebrew has no bottle for macOS 12 and the source build
fails (linker error). Per the resolution in `SPEC.md` Local dev environment: local dev uses SQLite
via a `DATABASE_URL` env var; MySQL (via PyMySQL) only runs in CI and production, wired up later.

**Acceptance criteria:**
- [x] `pip install -r requirements.txt && npm install` succeeds with all packages above present
- [x] `python manage.py runserver` serves the app directly on this Mac, no remote server needed —
  the root URL is a plain 404 rather than the default welcome page, because `urls.py` already
  wires in allauth/two-factor/debug-toolbar/silk URLs; `/admin/` and `/account/login/` both render
  with no server errors (see Verification note below for why `/admin/login/` redirects)
- [x] `DATABASES` reads from a `DATABASE_URL` env var, defaulting to SQLite locally
- [x] Sessions and cache are configured to use Redis (not file/local-memory) — stateless from day one

**Verification:**
- [x] `pytest` passes (3 smoke tests: admin login → 2FA login redirect, two-factor login page,
  allauth signup page)
- [x] `black --check . && ruff check .` passes
- [x] Manual check: `/admin/login/` redirects to `/account/login/` (two-factor's login) with no
  errors — this is `django-two-factor-auth` intentionally monkeypatching `AdminSite.login` to
  route admin auth through `LOGIN_URL`, which is set to `two_factor:login` in settings.py so admin
  auth goes through the 2FA-aware flow (the actual 2FA enforcement itself is still Task 6's job)

**Dependencies:** None

**Files likely touched:** `requirements.txt`, `package.json`, `.env.example`, `bancostore/settings.py`, `bancostore/asgi.py`, `bancostore/celery.py`

**Estimated scope:** M

---

### Task 2: Roles & permissions (Customer / Distributor / Admin)

**Description:** Set up three roles using Django's built-in groups/permissions. Add a `Distributor`
model (migration) for distributor-only fields (sponsor_id, rank, kyc_status, ir_id — nullable until
Task 10/11 fill them in). Seed one test user per role.

**Acceptance criteria:**
- [ ] Three groups exist: `customer`, `distributor`, `admin`
- [ ] `Distributor` model/migration exists with at least `user`, `sponsor`, `rank`, `kyc_status`
- [ ] A management command creates one user per role for local testing

**Verification:**
- [ ] pytest test: a user assigned the `distributor` group has a `Distributor` row and passes an `is_distributor()` check
- [ ] `python manage.py migrate` runs clean; seed command populates the three stub users

**Dependencies:** Task 1

**Files likely touched:** `apps/distributors/models.py`, `apps/distributors/migrations/*.py`, `apps/accounts/management/commands/seed_roles.py`, `apps/accounts/models.py`

**Estimated scope:** S

---

### Task 3: Settings foundation — seed all MVP-relevant business rules

**Description:** Set up `django-constance` config for the MVP-relevant Section 13 categories:
Authentication (13.1), Commission & Bonus (13.2), Registration & Membership (13.3), Withdrawal &
Payout (13.4), KYC (13.9), IR ID Number (13.13), Payment Gateway (13.14), General Platform (13.15).
Seed default values from `SPEC.md`/`docs/` Section 13 and 15 tables.

**Acceptance criteria:**
- [ ] `CONSTANCE_CONFIG` covers every field in the categories above, grouped by `CONSTANCE_CONFIG_FIELDSETS`
- [ ] Defaults match every value listed in Section 15 (Key Rules Summary)
- [ ] Settings are Redis-cached per `django-constance`'s Redis backend (no per-request DB hit)

**Verification:**
- [ ] pytest test: reading `constance.config.BINARY_BONUS_RATE` returns `7.5`
- [ ] pytest test: updating a setting value (e.g. via Django Admin) takes effect on the next read without a code change

**Dependencies:** Task 1

**Files likely touched:** `bancostore/settings.py` (`CONSTANCE_CONFIG`), `apps/platform_settings/*.py`, `apps/platform_settings/management/commands/seed_settings.py`

**Estimated scope:** M

---

**Checkpoint A:** `pytest` green, `black`/`ruff` clean, seeded stub users for all 3 roles, settings
readable.  Confirm before Phase 1.

---

## Phase 1: Authentication

### Task 4: Regular customer registration & login

**Description:** Full slice for regular customer auth: register with email or phone + password,
Google one-click login (django-allauth), guest checkout flag on the session, password reset via
email link or SMS OTP. **Needs email provider decision (Mailgun vs Gmail SMTP) before starting.**

**Acceptance criteria:**
- [ ] Customer can register with email/phone + password
- [ ] Customer can log in with Google via django-allauth
- [ ] Guest checkout path exists (no account required to reach checkout)
- [ ] Password reset flow works via email link

**Verification:**
- [ ] pytest feature test: register → log out → log in
- [ ] pytest feature test: password reset completes and new password works
- [ ] Manual check: Google login button redirects and returns a logged-in session locally

**Dependencies:** Task 2, Task 3, and the email-provider open question

**Files likely touched:** `apps/accounts/views.py`, `bancostore/urls.py`, `templates/accounts/*.html`, `tests/feature/accounts/test_customer_auth.py`

**Estimated scope:** M

---

### Task 5: Distributor registration & login

**Description:** Phone number + password login, SMS OTP verification during registration and for
password reset, account lockout after N failed attempts (from settings). **Needs SMS provider
decision (Arkesel vs Hubtel) and a working sandbox/test account before starting.**

**Acceptance criteria:**
- [ ] Distributor registers with phone + password; phone verified via OTP
- [ ] Login uses phone + password only (no Google login)
- [ ] Wrong-password lockout uses `django-constance` values, not hardcoded numbers

**Verification:**
- [ ] pytest feature test: registration + OTP verification + login
- [ ] pytest feature test: N failed logins locks the account for the configured duration
- [ ] Manual check: OTP is sent through the chosen SMS provider's sandbox/test mode

**Dependencies:** Task 2, Task 3, SMS provider decision

**Files likely touched:** `apps/distributors/views.py` (auth views), `apps/notifications/otp.py`, `tests/feature/distributors/test_distributor_auth.py`

**Estimated scope:** M

---

### Task 6: Admin login with mandatory 2FA

**Description:** Email + password login for admin via Django Admin, with mandatory 2FA
(django-two-factor-auth: SMS or authenticator app) that cannot be disabled. Failed attempts
trigger lockout + alert email.

**Acceptance criteria:**
- [ ] Admin cannot reach the admin panel without completing 2FA
- [ ] 2FA cannot be turned off through any settings toggle
- [ ] Lockout alert email fires on repeated failed attempts

**Verification:**
- [ ] pytest feature test: login without completing 2FA is rejected
- [ ] pytest feature test: login + correct 2FA code succeeds

**Dependencies:** Task 2, Task 3

**Files likely touched:** `bancostore/settings.py` (two-factor config), `apps/accounts/admin.py`, `tests/feature/accounts/test_admin_auth.py`

**Estimated scope:** S

---

**Checkpoint B:** all three roles register/login end to end, tests green.

---

## Phase 2: Catalog & Storefront

### Task 7: Product model + Django Admin CRUD

**Description:** Product model (name, description, photos, price GHS, PV value, category,
variants, stock, active/hidden, featured flag). Django Admin customization for full admin CRUD.

**Acceptance criteria:**
- [ ] Admin can create/edit/delete a product with photos (Pillow resize/WebP) via Django Admin
- [ ] Product has a PV value used only for distributor purchases
- [ ] Out-of-stock products show correctly (behavior stubbed per settings default)

**Verification:**
- [ ] pytest test: creating a product via Django Admin persists correctly
- [ ] pytest test: stock decremented on sale (basic case, full order flow comes in Phase 6)

**Dependencies:** Task 1, Task 3

**Files likely touched:** `apps/catalog/models.py`, `apps/catalog/admin.py`, `apps/catalog/migrations/*.py`

**Estimated scope:** M

---

### Task 8: Public storefront — home, listing, detail, search/filter/sort

**Description:** Home page (featured products, join/shop CTAs), product listing with search bar,
category filter, price range filter, sort (newest/price/popular), and product detail page, using
Django views + HTMX for reactive filtering without full-page reloads.

**Acceptance criteria:**
- [ ] Home page shows featured products from Task 7 data
- [ ] Search, category filter, and sort all work together on the listing page
- [ ] Product detail page shows photos, price, PV, stock state, add-to-cart button

**Verification:**
- [ ] pytest test: searching for a product name returns the right result
- [ ] pytest test: filtering by category narrows results correctly
- [ ] Manual check: visually confirm the storefront in browser

**Dependencies:** Task 7

**Files likely touched:** `apps/catalog/views.py`, `templates/catalog/*.html`, `tests/feature/catalog/*.py`

**Estimated scope:** M

---

**Checkpoint C:** admin-added product appears correctly on the public storefront.

---

## Phase 3: Distributor Core Domain (MLM)

### Task 9: Binary tree schema and placement/spillover service

**Description:** Build the `binary_tree_edges` closure table and `pv_ledger` aggregate table
(see `SPEC.md` Scale Architecture). Build the `BinaryTree` service: place a new distributor under
a sponsor's left/right leg, implement spillover when the direct slot is taken, and provide an
O(log n)/O(1) ancestor-aggregate query. No purchase/commission logic yet — schema and placement
only.

**Acceptance criteria:**
- [ ] Placing a distributor under a sponsor with a full leg triggers correct spillover to the next available slot
- [ ] Ancestor-aggregate lookup does not recursively walk the tree (verified, not just asserted)
- [ ] Closure table stays consistent after multiple placements (no orphaned or duplicate edges)

**Verification:**
- [ ] pytest test: spillover places a new distributor in the correct next-available position
- [ ] pytest test seeding a 10k+ node synthetic tree: ancestor query executes in constant/log-time query count (assert query count via Django's `django.db.connection.queries` / `assertNumQueries`, not wall-clock time)

**Dependencies:** Task 2

**Files likely touched:** `apps/binary_tree/models.py` (`BinaryTreeEdge`), `apps/pv_ledger/models.py` (`PvLedger`), `apps/binary_tree/services.py`, `tests/unit/binary_tree/*.py`

**Estimated scope:** L (schema-heavy; if it grows beyond ~5 files, split placement and the aggregate-query service into two tasks)

---

### Task 10: Distributor onboarding — registration fee, starter pack, placement, IR ID

**Description:** Full onboarding vertical slice: pay GHS 100 registration fee (Paystack sandbox) →
choose Starter Pack A or B → pay for it → get placed in the binary tree (Task 9's service) → PV
ledger updated up the ancestor chain at write time → IR ID generated per `django-constance` IR ID
settings (prefix/starting number/digits).

**Acceptance criteria:**
- [ ] Registration fee payment is non-refundable and gates account creation
- [ ] Starter pack choice sets the correct PV and rank (Bronze/Silver) from settings
- [ ] Distributor is placed in the tree and ancestor PV ledgers update immediately on purchase
- [ ] IR ID is generated once, is permanent, and follows the configured format

**Verification:**
- [ ] pytest feature test reproducing Section 14 steps 1–9 (through IR ID issuance, before KYC gating withdrawal)
- [ ] pytest test: PV ledger ancestor totals are correct immediately after a Pack B purchase

**Dependencies:** Task 5, Task 9, Paystack sandbox access

**Files likely touched:** `apps/pv_ledger/services.py`, `apps/distributors/views.py` (onboarding), `apps/orders/paystack_webhook.py`, `tests/feature/distributors/test_onboarding.py`

**Estimated scope:** L (if it grows past ~5 files, split payment handling from tree-placement-and-ledger)

---

### Task 11: KYC submission and admin review

**Description:** Distributor uploads Ghana Card (front/back), a selfie, and verifies phone via
OTP. Admin reviews and approves/rejects (with reason) via Django Admin. Withdrawal stays blocked
until KYC is approved.

**Acceptance criteria:**
- [ ] Distributor can submit all three KYC items
- [ ] Admin sees pending KYC submissions in Django Admin and can approve or reject with a reason
- [ ] Distributor cannot request a withdrawal until KYC status is approved

**Verification:**
- [ ] pytest feature test: KYC submission → admin approval → distributor's `kyc_status` flips to approved
- [ ] pytest test: withdrawal request is rejected while `kyc_status` is not approved

**Dependencies:** Task 5, Task 10

**Files likely touched:** `apps/distributors/kyc_services.py`, `apps/distributors/admin.py` (KYC review), `apps/distributors/views.py` (KYC submission), `tests/feature/distributors/test_kyc.py`

**Estimated scope:** M

---

**Checkpoint D:** a new distributor registers, pays, is placed with correct spillover, and passes
KYC to receive an IR ID — verified end to end through the UI.

---

## Phase 4: Commission Engine (elevated test rigor — see `SPEC.md` Testing Strategy)

### Task 12: Direct Referral Bonus — instant credit

**Description:** On starter-pack purchase confirmation, credit the sponsor's wallet with
`DIRECT_REFERRAL_BONUS_RATE` × the new distributor's PV, instantly, plus a notification.

**Acceptance criteria:**
- [ ] Rate is read from `django-constance` config, not hardcoded
- [ ] Credit happens the moment payment is confirmed (same request/Celery task, not delayed)
- [ ] Sponsor receives a notification with the correct amount and referred distributor's name

**Verification:**
- [ ] pytest test reproducing the doc example exactly: Pack B purchase → sponsor credited GHS 100 (10% of 1,000 PV)
- [ ] pytest test: rounding to the pesewa is correct on a non-round PV value

**Dependencies:** Task 10, Task 3

**Files likely touched:** `apps/commissions/services.py` (`DirectReferralCalculator`), `apps/wallet/services.py` (`WalletService`), `tests/unit/commissions/test_direct_referral.py`

**Estimated scope:** S

---

### Task 13: Binary Bonus task — every 10 minutes

**Description:** Celery Beat task reading `pv_ledger` aggregates per distributor: find the weak leg,
apply the rate, enforce the weekly cap, carry forward the stronger leg's excess (expiring after
`PV_CARRY_FORWARD_EXPIRY_DAYS`), and require 100 PV monthly personal activity to be eligible. Task
must only read pre-aggregated counters — never walk the tree.

**Acceptance criteria:**
- [ ] Weak leg correctly identified and reset to 0 after calculation; carry-forward applied to the strong leg
- [ ] Weekly GHS 50,000 cap enforced per distributor
- [ ] Distributors below 100 PV monthly personal activity are skipped
- [ ] Carried PV older than the expiry setting is dropped, not counted

**Verification:**
- [ ] pytest test reproducing the doc example exactly: left 1,500 / right 600 → weak leg 600 → GHS 45.00 bonus, 900 PV carried forward
- [ ] pytest test: weekly cap enforcement, zero/tie-leg case, expired carry-forward exclusion
- [ ] Scale test: seed 10k+ node tree, assert the task's DB query count does not grow with tree depth/width (reads aggregates only)

**Dependencies:** Task 9, Task 10, Task 3

**Files likely touched:** `apps/commissions/tasks.py` (`calculate_binary_bonus`, Celery Beat schedule), `apps/commissions/services.py` (`BinaryBonusCalculator`), `tests/unit/commissions/test_binary_bonus.py`

**Estimated scope:** L (if it grows past ~5 files, split carry-forward/expiry logic into its own service)

---

### Task 14: Matching Bonus task — weekly

**Description:** Weekly Celery Beat task: for each distributor, sum downline binary-bonus earnings
3 levels deep (Bronze) or unlimited depth (Silver), credit 5% of that total.

**Acceptance criteria:**
- [ ] Bronze distributors only earn matching bonus 3 levels deep; Silver earns unlimited depth
- [ ] Rate read from `django-constance` config
- [ ] Eligibility requires 100 PV personal purchase in the current month, same as Binary Bonus

**Verification:**
- [ ] pytest test reproducing the doc example exactly: Level 1 GHS 200→GHS10, Level 2 GHS150→GHS7.50, Level 3 GHS100→GHS5, total GHS22.50
- [ ] pytest test: a Silver-rank distributor earns from a Level 4+ recruit; a Bronze-rank distributor does not

**Dependencies:** Task 13

**Files likely touched:** `apps/commissions/tasks.py` (`calculate_matching_bonus`), `apps/commissions/services.py` (`MatchingBonusCalculator`), `tests/unit/commissions/test_matching_bonus.py`

**Estimated scope:** M

---

**Checkpoint E:** the Section 14 example journey's exact commission numbers reproduce in a single
feature test that runs registration → referral bonus → binary bonus → matching bonus.

---

## Phase 5: Wallet & Withdrawal

### Task 15: Wallet ledger

**Description:** Wallet model with an append-only ledger of credit/debit entries (referral,
binary, matching, withdrawal debit, refund reversal). Balance is derived from the ledger, not a
mutable counter. Earnings history view on the distributor dashboard.

**Acceptance criteria:**
- [ ] Every credit from Tasks 12–14 writes a ledger entry with type, amount, and timestamp
- [ ] Wallet balance is always the sum of ledger entries, never edited directly
- [ ] Earnings history view lists entries with type and amount

**Verification:**
- [ ] pytest test: balance after a mixed sequence of credits/debits matches the sum exactly
- [ ] pytest test: no code path outside `WalletService` writes to the ledger table directly

**Dependencies:** Task 12, Task 13, Task 14

**Files likely touched:** `apps/wallet/services.py` (`WalletService`), `apps/wallet/models.py` (`WalletLedgerEntry`), `apps/wallet/views.py` (earnings history), `tests/unit/wallet/test_wallet_service.py`

**Estimated scope:** M

---

### Task 16: Withdrawal request flow

**Description:** Distributor requests a withdrawal (above the configured minimum, once per week,
KYC-gated). System deducts withholding tax, admin reviews/approves (individually or in bulk), and
Paystack sends the payout (sandbox). **Needs min/max withdrawal amount + withdrawal day decided
before starting.**

**Acceptance criteria:**
- [ ] Withdrawal blocked if KYC is not approved, amount is below minimum, or one was already made this week
- [ ] Tax is deducted using the `WITHHOLDING_TAX_RATE` setting and shown to the distributor before confirming
- [ ] Admin can approve individually or in bulk; approved requests trigger a Paystack sandbox payout

**Verification:**
- [ ] pytest test reproducing the doc example exactly: GHS 500 requested → GHS 5 tax → GHS 495 paid
- [ ] pytest test: second withdrawal request in the same week is rejected

**Dependencies:** Task 11 (KYC), Task 15, withdrawal min/max/day decision, Paystack sandbox access

**Files likely touched:** `apps/withdrawal/services.py` (`WithdrawalService`), `apps/withdrawal/admin.py`, `apps/withdrawal/views.py` (withdrawal request), `tests/feature/withdrawal/test_withdrawal.py`

**Estimated scope:** L (if it grows past ~5 files, split the tax-calculation service from the Paystack payout integration)

---

**Checkpoint F:** a distributor requests a withdrawal, tax is deducted correctly, admin approves,
and a simulated Paystack payout succeeds.

---

## Phase 6: Checkout & Orders

### Task 17: Cart + checkout

**Description:** Cart review, delivery vs. pickup choice, delivery fee calculated by zone, order
summary, Paystack payment. Distributor purchases generate PV; regular purchases do not. **Needs
delivery zone fee table + free-delivery threshold decided before starting.**

**Acceptance criteria:**
- [ ] Delivery fee is correctly looked up by zone from settings, or free above the threshold
- [ ] A distributor's purchase generates PV and updates the ledger (Task 10's write path); a regular customer's does not
- [ ] Order confirmation is sent (SMS/email) on successful payment

**Verification:**
- [ ] pytest test: distributor purchase increments PV ledger; regular customer purchase does not
- [ ] pytest test: delivery fee matches the configured zone table, and is zero above the free threshold

**Dependencies:** Task 8, Task 10, delivery zone fee decision

**Files likely touched:** `apps/orders/views.py` (checkout), `apps/orders/services.py` (`DeliveryFeeCalculator`), `tests/feature/orders/test_checkout.py`

**Estimated scope:** M

---

### Task 18: Order status lifecycle + admin order management

**Description:** Order status stages (Pending → Confirmed → Processing → Dispatched → Delivered /
Cancelled / Refunded) with notifications on each change, and a Django Admin order management view
with filters, PDF invoice generation (WeasyPrint), and cancel/refund actions.

**Acceptance criteria:**
- [ ] Status transitions follow the stages in `SPEC.md` Section 5.2, each triggering a notification
- [ ] Admin can filter orders by status/date/customer and update status
- [ ] Unpaid orders auto-cancel after the configured hours

**Verification:**
- [ ] pytest test: status transition sequence and notification firing
- [ ] pytest test: an unpaid order older than the configured window is auto-cancelled by the scheduled Celery task

**Dependencies:** Task 17

**Files likely touched:** `apps/orders/models.py` (`Order`), `apps/orders/admin.py`, `apps/orders/tasks.py` (`auto_cancel_unpaid_orders`), `tests/feature/orders/test_order_lifecycle.py`

**Estimated scope:** M

---

**Checkpoint G:** both a customer and a distributor complete a purchase; PV is generated only for
the distributor; order status updates correctly with notifications.

---

## Phase 7: Cooling-Off Refund

### Task 19: 7-day cooling-off refund

**Description:** Within 7 days of joining, a distributor can cancel and get a refund: starter pack
price minus the configured processing fee, registration fee not refunded, PV removed from
upline's legs, any commissions already paid on this signup reversed.

**Acceptance criteria:**
- [ ] Refund amount matches the doc example exactly: Pack B GHS 2,000 → GHS 1,800 refunded (10% fee)
- [ ] PV added at signup is removed from every ancestor's ledger
- [ ] Any referral bonus paid to the sponsor for this signup is reversed

**Verification:**
- [ ] pytest test reproducing the doc example exactly
- [ ] pytest test: request after day 7 is rejected

**Dependencies:** Task 10, Task 12

**Files likely touched:** `apps/distributors/cooling_off_services.py`, `apps/distributors/views.py` (cancel membership), `tests/feature/distributors/test_cooling_off_refund.py`

**Estimated scope:** M

---

## Phase 8: Distributor Dashboard (real-time)

### Task 20: Dashboard core stats

**Description:** Django view + HTMX dashboard showing wallet balance, total earnings, this week's
earnings, team size, left/right leg PV, monthly personal PV, IR ID with copy button, referral
link with WhatsApp share, rank badge — updating live via Django Channels when underlying data
changes.

**Acceptance criteria:**
- [ ] All stats listed above render correctly for a seeded distributor
- [ ] Wallet balance updates without a page refresh when a commission is credited (Channels)

**Verification:**
- [ ] pytest test: dashboard view renders correct values from seeded data
- [ ] Manual check: credit a commission via the Django shell while dashboard is open, confirm live update

**Dependencies:** Task 15, Task 13

**Files likely touched:** `apps/distributors/views.py` (dashboard), `templates/distributors/dashboard.html`, `apps/distributors/consumers.py` (Channels), `tests/feature/distributors/test_dashboard.py`

**Estimated scope:** M

---

### Task 21: Binary tree view, earnings history, carry-forward tracker, notifications

**Description:** Visual binary tree diagram (name, IR ID, rank, PV per node), earnings history
list (Task 15 data), carry-forward PV tracker with expiry date, and a live notification bell
(new downline join, bonus credited, withdrawal approved, KYC status change, PV nearing expiry).

**Acceptance criteria:**
- [ ] Tree view renders the distributor's downline correctly using Task 9's closure table
- [ ] Notification bell updates live via Django Channels for each event type listed in `SPEC.md` 6.6

**Verification:**
- [ ] pytest test: tree view shows correct nodes for a seeded downline
- [ ] pytest test: each notification type is dispatched on its triggering event

**Dependencies:** Task 9, Task 20

**Files likely touched:** `apps/distributors/views.py` (tree view), `apps/notifications/models.py`, `tests/feature/distributors/test_notifications.py`

**Estimated scope:** M

---

**Checkpoint H:** dashboard and tree view update live (no page refresh) when a commission is
credited in another session.

---

## Phase 9: Admin — Remaining MVP Pieces

### Task 22: Admin user/distributor management

**Description:** Django Admin: search distributors/customers by name, IR ID, or phone; view full
profile (rank, sponsor, pack, join date, KYC status, wallet balance); suspend/deactivate account.

**Acceptance criteria:**
- [ ] Search works across name, IR ID, and phone
- [ ] Suspend/deactivate blocks login for that account

**Verification:**
- [ ] pytest test: search returns the correct distributor by IR ID
- [ ] pytest test: a suspended distributor cannot log in

**Dependencies:** Task 11

**Files likely touched:** `apps/distributors/admin.py`, `tests/feature/admin/test_distributor_management.py`

**Estimated scope:** S

---

### Task 23: Admin commission oversight

**Description:** Django Admin view: every commission calculation (who/what/when/which leg), which
distributors hit the weekly cap, full binary tree view for any distributor, PV batches nearing
180-day expiry.

**Acceptance criteria:**
- [ ] Admin can see a full commission log for any distributor
- [ ] Weekly-cap-hit distributors are visible in a filtered view

**Verification:**
- [ ] pytest test: a distributor who hit the weekly cap appears in the capped-list query

**Dependencies:** Task 13, Task 22

**Files likely touched:** `apps/commissions/admin.py` (commission log), `tests/feature/admin/test_commission_oversight.py`

**Estimated scope:** S

---

**Checkpoint I (final MVP checkpoint):** the full Section 14 distributor journey works end to end
through the UI. All pytest tests pass, `black`/`ruff` clean. Verify against `SPEC.md` Success
Criteria before calling the MVP done.

---

## Phase 10: Deployment

### Task 24: Deploy to Hostinger VPS (production)

**This is a production deployment — confirm with the user before running any step against the
real VPS or domain**, per `SPEC.md` Boundaries (production deploys are an "ask first" action).

**Description:** Stand up the Hostinger KVM 2 VPS (Ubuntu 22.04 LTS) as the production host and
move Bancostore onto it: MySQL 8 (real concurrent writes, replacing local SQLite), Redis, Nginx as
reverse proxy + static/media file server, Let's Encrypt for HTTPS, and Supervisor to keep Daphne
(ASGI) and the Celery worker/beat processes running permanently, including across reboots. This is
the point where every "local dev uses SQLite / MySQL doesn't run on this Mac" workaround in
`SPEC.md` stops applying — production runs the real stack end to end.

**Acceptance criteria:**
- [ ] Hostinger KVM 2 VPS provisioned (Ubuntu 22.04 LTS), SSH key-based access configured, root
  login disabled in favor of a sudo user
- [ ] MySQL 8, Redis, Nginx, and Python installed on the VPS via `apt` (native install works here —
  unlike this Mac, Ubuntu 22.04 has current bottles/build tools for all of these)
- [ ] Production `.env` created directly on the server (never committed): real `SECRET_KEY`,
  `DEBUG=False`, `ALLOWED_HOSTS` set to the production domain, `DATABASE_URL` pointing at the VPS's
  MySQL, `REDIS_URL`, and the Paystack/email/SMS provider keys from `SPEC.md` Open Questions
- [ ] `pip install -r requirements.txt`, `npm run build`, `python manage.py collectstatic`, and
  `python manage.py migrate` all run clean against real MySQL on the VPS
- [ ] Supervisor configs for Daphne, `celery worker`, and `celery beat` — auto-restart on crash and
  on VPS reboot
- [ ] Nginx reverse-proxies to Daphne, serves `static/`/`media/` directly, and Let's Encrypt issues
  a valid HTTPS certificate (with auto-renewal) for the production domain
- [ ] A deploy process is documented (manual runbook at minimum; GitHub Actions auto-deploy on
  push to `main` if the CI provider from Open Question #1 is confirmed by this point)

**Verification:**
- [ ] Visiting the production domain over HTTPS loads the app with no errors
- [ ] `sudo supervisorctl status` shows Daphne, Celery worker, and Celery beat all `RUNNING` after
  a `sudo reboot` of the VPS
- [ ] One full smoke-test purchase (mirroring Checkpoint I) succeeds against real MySQL/Redis on
  the VPS, before any real user account exists on it — per `SPEC.md` Boundaries, never test or
  develop against the live production server/database once real users and real money are on it

**Dependencies:** Task 23 (Checkpoint I — full MVP complete and verified locally), Open Question #1
(CI provider confirmed)

**Files likely touched:** a new `deploy/` directory (Nginx site config, Supervisor program
configs), `.env.example` (document the production-only variables), `SPEC.md` Commands section
(add the verified production commands, matching how Task 1 updated the local dev commands),
possibly `.github/workflows/deploy.yml`

**Estimated scope:** L
