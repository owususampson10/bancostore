# Spec: Bancostore Platform (MVP Core)

## Objective

Bancostore is an online direct-selling platform for Ghana combining an ecommerce store (watches,
jewellery, perfumes) with a binary-MLM earning system. Two user types shop; a third type
("distributor" / Independent Representative) can additionally earn commissions by selling and
recruiting.

**Who it's for:**
- Regular customers — shop, checkout as guest or with an account. No commissions.
- Distributors — pay a registration fee + starter pack, get an IR ID, get placed in a binary tree,
  and earn Direct Referral, Binary, and Matching bonuses automatically.
- Admin — runs the business: KYC review, withdrawal approval, product/order management, and
  controls all business rules (rates, caps, fees, thresholds) through a settings panel with no
  code changes.

**Why it matters / what "correct" means here:** this is a financial platform. Commission math
runs unattended every 10 minutes and moves real money (Mobile Money/bank payouts via Paystack,
minus GRA withholding tax). A wrong formula costs real money and erodes trust in the IR ID /
wallet system the entire distributor relationship is built on.

**Scale target:** this is a long-term project, not a scope-and-done MVP. The expected long-run
scale is hundreds of thousands of users/distributors. Feature scope below is still cut down to
MVP (see below), but the data model and job design for the binary tree and commission engine are
built for that scale from the start — retrofitting an event-driven PV ledger onto a live
financial system later is far riskier than building it correctly now. Confirmed: no native
mobile app on the roadmap (mobile web only), so the Django + HTMX server-rendered monolith is
the long-term frontend architecture, not just an MVP shortcut. Infrastructure stays VPS-based to
control cost, with a documented path to a horizontally-scaled topology (see Tech Stack).

**MVP scope (this spec):** the smallest slice that lets a real distributor journey work
end-to-end — register, get placed in the tree, generate PV, earn all three bonus types, pass
KYC, and withdraw. Deferred to a later spec: sales/revenue reporting, compliance dashboard
(70% retail ratio, escrow tracker), discount codes/promotional banners, product reviews,
backorders, PDF invoice export, and CSV/PDF report exports. The Admin Settings Panel is
in-scope but limited to the settings sections that gate MVP behavior (13.1–13.4, 13.9, 13.13,
13.14, 13.15) — sections 13.5–13.8, 13.10–13.12 are stubbed with sane hardcoded defaults and
revisited in the next phase.

### In scope (MVP)
- Section 1 — Home page, product pages, search/filter, two purchase types (regular vs. distributor PV)
- Section 2 — Authentication for all 3 roles (customer, distributor, admin) incl. mandatory admin 2FA
- Section 3/4 (customer accounts, distributor onboarding incl. registration fee, starter pack, KYC, IR ID, binary tree placement + spillover)
- Section 5 — Checkout, delivery/pickup, order status lifecycle
- Section 6 — Distributor dashboard (wallet, PV, team size, binary tree view, earnings history, notifications)
- Section 7 — All three commission engines: Direct Referral (instant), Binary (every 10 min, weak-leg, carry-forward, 180-day expiry), Matching (weekly, 3-level Bronze / unlimited Silver)
- Section 8 — E-wallet + withdrawal flow incl. withholding tax deduction and Paystack payout
- Section 9 — 7-day cooling-off refund
- Section 10.1/10.2 — Product & inventory management (reviews deferred)
- Section 12.1–12.3 — Admin: user/distributor management, commission oversight, withdrawal management (reporting/compliance dashboards deferred)
- Section 13 (subset above) — Admin Settings Panel for MVP-relevant values

### Out of scope (defer to Phase 2 spec)
- Sales/revenue/compliance reporting (12.4–12.6)
- Discount codes & promotional banners (Section 11)
- Product reviews (10.3)
- Backorders
- PDF/CSV export tooling
- Full notification template editor, multi-provider SMS/email switching UI (hardcode one provider each for MVP)

## Tech Stack

Originally specified as Laravel/PHP per `docs/Bancostore_Features_and_Workflow_v4.docx` Section 16.
**Switched to Python/Django** after Laravel/PHP proved unable to install natively on this
machine's macOS 12.7.6 Monterey (see Local dev environment below) — this is now the fixed stack,
not open for substitution without discussion (see Boundaries):

| Layer | Technology |
|---|---|
| Hosting | Hostinger KVM 2 VPS, Ubuntu 22.04 LTS, Nginx, Let's Encrypt |
| Backend framework | Django 5, Python 3.12, pip/Poetry |
| Frontend | Django Templates + HTMX, Alpine.js, Tailwind CSS v4, Vite |
| Real-time | Django Channels + Daphne (ASGI), Redis channel layer |
| Database | MySQL 8 in CI and production (via PyMySQL — pure Python driver); SQLite for local dev only (see below), Django ORM, Django Migrations |
| Cache/Queue | Redis, Celery, Celery Beat (scheduled jobs), Flower (monitoring) |
| Auth | django-allauth (registration/login + Google), django-two-factor-auth (mandatory admin 2FA), Django's built-in auth groups/permissions for the three roles |
| Admin panel | Django Admin (customized) |
| Settings | django-constance (DB-backed, admin-editable business rules) |
| Files | Django FileField/ImageField (local disk storage), Pillow |
| Payments | Paystack REST API via `requests` (no official first-party Python SDK) |
| Email/SMS | Django email backend (Gmail SMTP, confirmed 2026-07-10 — revisit for Mailgun before real volume, see Local dev environment), mNotify via `requests` (confirmed 2026-07-11) |
| PDF/Audit | WeasyPrint, django-simple-history |
| Testing | pytest + pytest-django |
| Local dev | Native on this Mac via `pyenv` — no remote server required (see below) |
| Process manager | Supervisor (unchanged — keeps Celery workers and Daphne running permanently, same role as it had with Laravel Queues/Reverb) |
| Icons | Heroicons (SVG) via a small Django template tag |
| WS client (browser) | Native browser `WebSocket` API + a small reconnect helper (Channels has no bundled client like Laravel Echo) |
| Dev debugging | `django-debug-toolbar` |
| Query/perf monitoring | `django-silk` (query profiling) + Flower (queue health) — no single package matches Laravel Pulse exactly |
| Rate limiting | `django-ratelimit` |
| Field-level encryption | `django-cryptography` (Fernet-based) |

**Two things that are trade-offs, not renames** — the Django side needs real work here, not just a
package swap:
- **Filament is Livewire-powered, so it's real-time by default; Django Admin is not.** Any admin
  screen that needs to update live (e.g. the withdrawal requests panel appearing the instant a
  distributor submits one) needs its own Channels wiring — it doesn't come free like it did with
  Filament.
- **Laravel Notifications** (one class dispatching to email+SMS+in-app at once) has no Django
  package equivalent. Build this as a small custom service in `apps/notifications/services.py` —
  budget real task time for it, don't assume a pip install covers it.
- **Livewire File Upload** (progress bar + preview, built into Livewire) also has no drop-in
  Django equivalent. Product photo upload (Task 7) and KYC document upload (Task 11) need a small
  custom HTMX + Alpine.js implementation for the progress/preview behavior — budget for it in
  those tasks rather than assuming a package handles it.

**Local dev environment:** the original plan was Laravel/PHP, but every local macOS path for PHP
was ruled out, in order: Laravel Herd (its PHP needs a newer macOS than this machine's 12.7.6
Monterey), native Homebrew PHP (hit a repeatable, non-transient `oniguruma` build failure), Docker
Desktop (refuses to install below macOS Sonoma), and Colima+QEMU (QEMU requires Xcode Clang 15,
macOS 13+). Getting a remote Ubuntu dev server working (Oracle Cloud Free Tier) also stalled on
SMS/phone verification for hours. Rather than keep fighting either problem, the stack switched to
Python/Django, which installs cleanly on old macOS via `pyenv` — CPython and most PyPI packages
(Django, Celery, channels-redis, Pillow) ship as pure Python or precompiled wheels, avoiding the
kind of native C-extension build failure that blocked PHP.

`redis` installed cleanly via Homebrew as expected (it was already present on this machine).
**`mysql` did not** — Homebrew has no precompiled bottle for MySQL on macOS 12, so it tried to
compile from source and hit a linker failure (`-no_warn_duplicate_libraries`, an old-linker/new-flag
mismatch) — the same class of problem PHP hit, just with the database server instead of the
language runtime. `mysql@8.0` and `mariadb` were considered as alternatives but not pursued once
the simpler fix below was agreed.

**Resolution: MySQL never runs on this Mac at all.** Local development uses **SQLite** (built into
Python, zero install) via Django's `DATABASE_URL` env var, defaulting to `sqlite:///db.sqlite3`
locally. Real MySQL only runs in two places: **GitHub Actions CI** (Linux runners pull the
official `mysql:8` Docker image with no compile step — this is where the commission/wallet/PV-ledger
elevated-rigor tests actually run against MySQL, see Testing Strategy) and **production** (the
Hostinger VPS, purchased later — not needed yet since nothing is deployed). The Hostinger VPS is
*not* being provisioned early to double as a dev database, to avoid an unnecessary purchase this
early; revisit that once we're closer to needing a persistently-running environment.

SQLite and MySQL differ in ways that matter for this app — most notably, SQLite locks the whole
database file per write (single-writer) while MySQL allows real concurrent row-level writes. Since
Bancostore's core design writes to PV ledgers and wallet balances concurrently under load, a
concurrency bug could pass locally on SQLite (which serializes writes) and only surface under real
MySQL. This is exactly why the CI-with-real-MySQL step above is not optional for the
Commission/Wallet/Withdrawal test suite, even though day-to-day local dev uses SQLite.

**Use `PyMySQL` as the MySQL driver wherever MySQL is used** (CI, production) — `mysqlclient` needs
a C compiler and MySQL dev headers at install time, which risks repeating the same class of problem
just hit; `PyMySQL` is pure Python and configured as `MySQLdb`'s drop-in replacement in Django.

### Scale Architecture (built in from the start, not deferred)

Framework choices above don't change for scale — Django/MySQL/Redis/Paystack all hold at
hundreds of thousands of users. What must be designed correctly from the first migration/service,
because retrofitting later is far more expensive than building it right now:

- **App servers are stateless.** No local file session storage, no in-process-only cache or
  counters. Sessions and cache go through Redis so any app node can serve any request — this is
  what makes "add another VPS behind a load balancer" a config change later, not a rewrite.
- **Binary tree storage uses a closure table (or nested set), not naive parent-pointer recursion.**
  Subtree PV aggregation must be O(log n) or O(1) lookups, not a recursive walk of the whole
  downline per distributor per cycle.
- **PV/commission calculation is event-driven, not a full recompute.** Every purchase event
  increments leg-PV aggregate counters (via a `pv_ledger` table) for every ancestor up the tree
  at write time. The 10-minute job then only evaluates already-current aggregates against the
  weak-leg formula and cap — it never re-walks the tree from scratch. This is the single
  highest-risk piece to get wrong at scale and the one place where the MVP build must already
  reflect the long-term design.
- **Celery workers and Channels/Daphne are designed to run on more than one process/node.**
  Supervisor configs and queue names are structured so additional workers or Channels instances can
  be added without code changes — actually adding those nodes is a later infra step, not a day-one
  requirement.
- **Scale path (VPS-based, not cloud-native):** start on one Hostinger VPS. When load requires it,
  the documented migration is: move MySQL to its own VPS (or managed MySQL) with read replicas →
  add a load balancer in front of 2+ app VPS nodes → move Redis to its own VPS/cluster → run
  multiple Reverb nodes behind the load balancer with sticky sessions. None of this requires
  changing application code if the statelessness rule above is followed.

## Commands

To be finalized once the project is scaffolded (`django-admin startproject bancostore`). Expected
shape based on the stack above:

```
(run natively on this Mac via pyenv — see Local dev environment above)

Install:       pip install -r requirements.txt && npm install
Dev server:    python manage.py runserver 0.0.0.0:8000
ASGI/Channels: daphne -b 0.0.0.0 -p 8001 bancostore.asgi:application
Dev assets:    npm run dev
Build assets:  npm run build
Migrate:       python manage.py migrate
Seed:          python manage.py seed_data
Test:          pytest
Test (unit):   pytest -k commission
Lint/format:   black . && isort . && ruff check .
Celery worker: celery -A bancostore worker -l info
Celery beat:   celery -A bancostore beat -l info
Flower:        celery -A bancostore flower
```

## Project Structure

Standard Django project layout, split into per-domain apps:

```
bancostore/                  → Django project package (settings.py, urls.py, asgi.py, celery.py)
apps/
  accounts/                  → User model, roles/groups, auth views (customer/distributor/admin)
  distributors/               → Distributor model, KYC, IR ID
  binary_tree/                 → Placement + spillover logic, closure-table maintenance
  pv_ledger/                    → Event-driven PV aggregation on every purchase (updates ancestor leg totals)
  commissions/                  → DirectReferralCalculator, BinaryBonusCalculator, MatchingBonusCalculator, Celery tasks
  wallet/                        → WalletService (credit/debit, ledger entries)
  withdrawal/                     → WithdrawalService (tax calc, Paystack payout)
  catalog/                         → Product model, storefront views
  orders/                           → Cart, checkout, order lifecycle
  notifications/                    → SMS/email/in-app notification classes
  platform_settings/                 → django-constance config, business-rule defaults (Section 13/15)
templates/                    → Django templates + HTMX partials (dashboard, cart, checkout, binary tree view)
static/                        → Tailwind/Alpine/Vite-built assets
tests/
  feature/                    → End-to-end flows (registration, checkout, withdrawal)
  unit/commissions/           → Commission math — the highest-scrutiny test directory
docs/                         → Source requirements (this repo's ground truth alongside SPEC.md)
```

Each `apps/<name>/` follows Django convention: `models.py`, `views.py`, `admin.py` (Django Admin
customization, replacing what would have been a Filament resource), `services.py` (business logic,
kept out of views/models), `tasks.py` (Celery tasks, e.g. `commissions/tasks.py` for the 10-minute
binary bonus job), `migrations/`.

## Code Style

- Formatting via `black . && isort . && ruff check .` — run before every commit, no manual style debates
- PEP 8 + Django conventions: singular model class names, plural table names (Django default), `snake_case` everywhere (no `camelCase`)
- Money is always integer minor units (pesewas) or Python `Decimal` — never raw `float` for GHS amounts, to avoid rounding drift in commission math
- All business-rule values (rates, caps, fees, thresholds) are read from `django-constance` config — never hardcoded as literals in a service or task

Example — a commission calculator reads its rate from settings, not a literal:

```python
from decimal import Decimal
from constance import config as settings

class BinaryBonusCalculator:
    def calculate(self, distributor: "Distributor") -> "BinaryBonusResult":
        weak_leg_pv = min(distributor.left_leg_pv(), distributor.right_leg_pv())

        bonus = weak_leg_pv * (Decimal(settings.BINARY_BONUS_RATE) / 100)

        return BinaryBonusResult(
            weak_leg_pv=weak_leg_pv,
            bonus_amount=Money.from_ghs(bonus),
            capped_at=settings.WEEKLY_BINARY_BONUS_CAP,
        )
```

## Testing Strategy

- Framework: pytest + pytest-django, `tests/feature/` and `tests/unit/`
- **Commission engine, wallet, and withdrawal code (`apps/commissions/services.py`,
  `apps/wallet/services.py`, `apps/withdrawal/services.py`) requires tests for every code path
  before merge** — this is the elevated-rigor area agreed on. Cases to cover per calculator:
  weak-leg selection, carry-forward across cycles, 180-day expiry, weekly cap enforcement,
  zero/tie legs, rounding to the pesewa, monthly-100-PV eligibility gate, tax deduction rounding.
- Feature tests cover full user journeys: distributor registration → KYC → tree placement →
  purchase → commission credit → withdrawal request → payout, mirroring Section 14's example.
  Use Paystack's test mode / a fake gateway — never hit live payment or SMS providers in tests.
- Standard coverage (happy path + obvious edge cases) elsewhere: catalog, cart, order status
  transitions, admin CRUD.
- CI runs `pytest` and `black --check . && ruff check .` on every push (to be wired up once
  a CI provider is chosen — currently undecided, see Open Questions).
- **CI must run the commission/wallet/withdrawal test suite against a real MySQL service
  container, not SQLite** — local dev uses SQLite (see Local dev environment above), and SQLite's
  single-writer locking can hide concurrency bugs in the PV-ledger/wallet write paths that only
  show up under MySQL's real concurrent writes. This is the mitigation for that gap, not optional.

## Boundaries

- **Always do:**
  - Run `pytest` and `black --check . && ruff check .` before considering a task done
  - Read business-rule values from `django-constance` config, never hardcode them
  - Write/extend pytest tests alongside any change to commission, wallet, or withdrawal code
  - Use integer/pesewa-safe money handling (`Decimal`), never float arithmetic for GHS amounts
  - Log admin actions affecting money or KYC via `django-simple-history`
  - Never develop or test against the live Hostinger production server/database once Bancostore
    has real users and real money moving through it
  - Design new tables/services to be stateless and horizontally scalable — no local-only state,
    no in-process-only counters, no naive full-tree recursion for PV aggregation (see Scale
    Architecture)

- **Ask first:**
  - Adding any pip/npm dependency not already named in the Tech Stack table above
  - Changing seeded default commission rates, caps, fees, or the underlying formulas in code
    (even though these are meant to be admin-editable at runtime)
  - Any database schema/migration change after the initial MVP schema is in place and reviewed
  - Writing or modifying Paystack, mNotify (or any payment/SMS provider) integration code —
    including in test/sandbox mode
  - Anything in the general defaults: secrets, production deploys, destructive git operations,
    force pushes, CI config changes

- **Never do:**
  - Commit API keys/secrets (Paystack, SMS, email) — these are stored encrypted at rest via
    `django-constance`/environment variables, sourced from `.env` locally, never literals in code
  - Use floats for money math anywhere in the commission/wallet path
  - Auto-approve withdrawals, KYC, or bypass the 2FA requirement for admin, even temporarily for
    "testing convenience" in code that could reach a shared branch
  - Remove or weaken a passing commission/wallet test without explicit approval

## Success Criteria

- A distributor can complete the full Section 14 journey end-to-end in the local dev environment
  on this Mac (or the Hostinger VPS): register → pay registration fee (Paystack test mode) →
  choose starter pack → get placed in the binary tree (with spillover working) → complete KYC →
  get an IR ID → have referrals generate Direct Referral bonus instantly → have the Binary Bonus
  job calculate correctly on a 10-minute schedule → have Matching Bonus calculate weekly → request
  a withdrawal → see tax deducted correctly → receive a simulated Paystack payout
- Every commission/wallet/withdrawal code path has a passing pytest test, including the edge cases
  listed in Testing Strategy
- The 10-minute binary bonus job's cost does not scale with total downline size per distributor —
  verified with a test seeding a deep/wide synthetic tree (e.g. 10k+ nodes) and asserting the job
  reads pre-aggregated PV counters rather than walking the tree
- Admin can log in with mandatory 2FA and adjust an MVP-scoped setting (e.g. binary bonus rate)
  and see it take effect without a code deploy
- `pytest` and `black --check . && ruff check .` both pass with zero failures

## Open Questions

1. CI provider — GitHub Actions assumed but not confirmed (repo has no remote yet)
2. ~~Which SMS provider to actually wire up first for MVP — Arkesel or Hubtel?~~ — resolved
   2026-07-11: **mNotify**, not either of the two originally named in the docs. User has an API
   key already; needed before Task 5 starts.
3. ~~Which email provider first — Mailgun or Gmail SMTP?~~ — resolved 2026-07-10: **Gmail SMTP**,
   verified working end-to-end with a real send (see `SPEC.md` Local dev environment and
   `tasks/todo.md` Task 4). Revisit for Mailgun before real production volume — Gmail's free-tier
   sending cap (500/day) and deliverability aren't built for that.
4. Exact delivery zone fee table and free-delivery threshold values (docs say "set by admin" —
   need real starting numbers to seed)
5. Minimum/maximum withdrawal amount and withdrawal day (also "set by admin" in docs — need
   concrete seed values)
6. ~~Is there an existing Paystack/mNotify account already, or do these need to be created before
   integration work can start?~~ — resolved: mNotify confirmed, API key in hand (2026-07-11);
   Paystack confirmed, both test and live API keys in hand (2026-07-13).
7. ~~Confirm `pyenv`-installed Python + Homebrew `mysql`/`redis` actually install cleanly on this
   Mac~~ — resolved during Task 1: Python/Redis installed cleanly, MySQL did not (see Local dev
   environment above).
