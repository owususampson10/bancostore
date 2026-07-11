# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

**Task 1 (scaffold) is done.** The Django 5 project is scaffolded at the repo root (`manage.py`,
`bancostore/` project package), the full stack from `SPEC.md` Tech Stack is installed and wired up
in `bancostore/settings.py` (Redis-backed cache/sessions, Channels/ASGI, Celery, constance,
allauth, two-factor-auth, simple-history, debug-toolbar, silk), and Tailwind v4 + Alpine.js + htmx
are wired through Vite (`vite.config.js`, `static/src/`). No per-domain `apps/` exist yet — that
starts with Task 2 (Roles & permissions). See `tasks/plan.md` and `tasks/todo.md` for the full task
breakdown and what's next.

**Environment note:** `cbor2` (a transitive dep of `daphne`/`autobahn`) is pinned to `5.5.0` in
`requirements.txt` — later versions need a Rust compiler to build, which isn't available on this
Mac (same class of native-build issue as the PHP/MySQL problems below). 5.5.0 ships a pure-Python
wheel. `phonenumbers` and `PyJWT` are also pinned in requirements.txt as required-but-undeclared
transitive deps of `django-two-factor-auth`'s phone plugin and allauth's Google provider.

Read `SPEC.md` in full before starting work — it contains the MVP scope, the full tech stack,
project structure, code style, testing strategy, and the Boundaries (Always/Ask first/Never)
that govern this repo. This file summarizes and highlights; `SPEC.md` is authoritative if the two
ever disagree.

**Stack history:** the original plan was Laravel/PHP. That was dropped after PHP could not be made
to run locally on this Mac (macOS 12.7.6 Monterey — Herd, native Homebrew PHP, and Docker all
failed for OS-version reasons), and getting a remote Ubuntu dev server going also stalled for
hours on Oracle Cloud's phone verification. The stack is now Python/Django, chosen because it
installs cleanly on this Mac via `pyenv` with no native-extension build issues. See `SPEC.md` Tech
Stack section for the full rationale and the Django-equivalent of every Laravel/Filament/Livewire
piece.

## What Bancostore Is

A Ghana-based ecommerce + binary-MLM platform. Three user types: regular customers (shop, no
commissions), distributors (Independent Representatives who register with an IR ID, get placed in
a binary tree, and earn three types of commission), and admin (controls every business rule
through a settings panel, no code changes needed). It is a **financial platform** — a background
job recalculates commissions every 10 minutes and moves real money via Paystack payouts minus GRA
withholding tax. This is also a long-term project targeted at hundreds of thousands of users, not
a throwaway MVP — see "Scale Architecture" in `SPEC.md` for the data-model implications of that.

## Commands (verified working)

Dev happens natively on this Mac (no `pyenv` needed — Python 3.13 is already installed) — no
remote server required. See `SPEC.md` Local dev environment for why (Python installs cleanly on
macOS 12 where PHP could not). Activate the venv first: `source venv/bin/activate`.

```
pip install -r requirements.txt && npm install         # install
python manage.py runserver 0.0.0.0:8000                # dev server
daphne -b 0.0.0.0 -p 8001 bancostore.asgi:application   # ASGI/Channels (WebSockets)
npm run dev                                             # dev assets (watch mode)
npm run build                                           # build assets
python manage.py migrate
python manage.py seed_data                              # not yet implemented (Task 2+)
pytest                                                  # full suite
pytest -k commission                                    # single test / group (Task 12+)
black . && isort . && ruff check .                      # format (run before every commit)
celery -A bancostore worker -l info
celery -A bancostore beat -l info
celery -A bancostore flower
```

Redis must be running locally (`redis-server` via Homebrew) for cache/sessions/Channels/Celery to
work — it's already installed on this Mac. Local dev config lives in `.env` (gitignored; copy from
`.env.example`).

## Architecture

**Stack**: Django 5 / Python 3.13 (whatever's already on this Mac — no `pyenv` needed), Django
Templates + HTMX + Alpine.js + Tailwind v4 (server-rendered, no separate SPA/mobile app —
confirmed out of scope), Django Admin for the admin panel, SQLite locally / MySQL 8 in CI and
production (via PyMySQL), Redis (cache/Celery broker), Django Channels for WebSockets, Paystack
for payments, `django-constance` for all runtime-configurable business rules, Django's built-in
groups/permissions for the three user roles. Full rationale for each choice is in `SPEC.md` Tech
Stack. **Note:** MySQL does not run locally on this machine (no Homebrew bottle for macOS 12) —
local dev uses SQLite, and the commission/wallet/PV-ledger tests must run against real MySQL in CI
to catch concurrency bugs SQLite's locking would hide (see `SPEC.md` Testing Strategy).

**The one non-obvious architectural constraint**: the binary tree and commission engine must be
built as **event-driven from the start**, not deferred as a "make it scale later" concern. Every
purchase increments PV aggregate counters up the tree at write time (`apps/pv_ledger/`,
`pv_ledger` table); the tree itself is a closure table (`binary_tree_edges`), not parent-pointer
recursion. The 10-minute binary bonus Celery task only reads pre-aggregated counters — it never
walks the tree. This is deliberate: retrofitting this onto a live financial ledger later is far
riskier than building it correctly now. Do not implement the naive "recompute full leg PV by
walking the tree every cycle" version described informally in `docs/` — that was superseded by the
Scale Architecture section of `SPEC.md`.

**Business rules live in settings, not code.** Commission rates, caps, fees, delivery zones, tax
rates, KYC requirements, etc. are all read from `django-constance` config
(`apps/platform_settings/`), seeded from the values in `SPEC.md` / `docs/`. Never hardcode a rate
or cap as a literal in a service or Celery task.

**Money is always integer/pesewa-safe** (`Decimal`, never `float`), anywhere in the
commission/wallet/withdrawal path.

Directory layout, full settings categories, and the complete MVP feature scope (what's in vs.
explicitly deferred to a Phase 2 spec) are documented in `SPEC.md` — refer there rather than
duplicating it here.

## Agent Skills Workflow

Every task in this repo goes through the `agent-skills` plugin — the matching skill invoked fresh
at the point it applies, every time, not once per session. Skill guidance loaded earlier fades
(especially after context compaction), so "I already called this once" is never a reason to skip
re-invoking it.

- **Before a build**: if the task isn't already scoped, `planning-and-task-breakdown` (or
  `spec-driven-development` if requirements are unclear). For unfamiliar or high-stakes changes
  (auth, money, migrations, irreversible operations), `doubt-driven-development`.
- **During a build**: `incremental-implementation` (small slices) + `test-driven-development`
  (test before/with the code), always. Add `frontend-ui-engineering` for UI work,
  `security-and-hardening` for anything touching auth/input/external integrations,
  `source-driven-development` when correctness depends on a framework's documented behavior.
- **After every build, no matter how small**: `debugging-and-error-recovery` if anything breaks
  (root-cause it, don't guess), `code-review-and-quality` before calling the change done, and
  `code-simplification` if the result is more complex than it needs to be. Run the full test
  suite, not just the new tests, before moving to the next task.

Draw from the full 24-skill catalog as the activity actually calls for it — not a default few,
and not all 24 on every task: `api-and-interface-design`, `browser-testing-with-devtools`,
`ci-cd-and-automation`, `code-review-and-quality`, `code-simplification`, `context-engineering`,
`debugging-and-error-recovery`, `deprecation-and-migration`, `documentation-and-adrs`,
`doubt-driven-development`, `frontend-ui-engineering`, `git-workflow-and-versioning`,
`idea-refine`, `incremental-implementation`, `interview-me`, `observability-and-instrumentation`,
`performance-optimization`, `planning-and-task-breakdown`, `security-and-hardening`,
`shipping-and-launch`, `source-driven-development`, `spec-driven-development`,
`test-driven-development`, `using-agent-skills`.

## Testing

pytest + pytest-django. `apps/commissions/services.py`, `apps/wallet/services.py`, and
`apps/withdrawal/services.py` require tests for every code path before merge (weak-leg selection,
carry-forward, 180-day expiry, weekly cap, rounding, monthly-100-PV eligibility) — this is the one
area with elevated rigor beyond standard coverage. Never hit live Paystack/SMS providers in tests;
use test mode / fakes.

## Boundaries

Full detail in `SPEC.md` Boundaries section. The categories that require asking first before
acting, beyond the general secrets/prod-deploy/destructive-git defaults:
- Adding any pip/npm dependency not already named in the Tech Stack
- Changing seeded default commission rates/caps/fees or their formulas in code
- Any database schema/migration change after the initial MVP schema is reviewed
- Writing or modifying Paystack/mNotify integration code, including in sandbox mode
