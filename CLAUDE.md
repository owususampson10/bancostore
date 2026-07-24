# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

**Tasks 1–16 are done — Phase 5 (Wallet ledger + Withdrawal request flow) is complete; Task 17
(Cart + checkout, opening Phase 6) is next.** What exists and is verified working:

- **Foundation (Tasks 1–3):** Django 5 scaffold with the full `SPEC.md` stack wired up in
  `bancostore/settings.py` (Redis-backed cache/sessions, Channels/ASGI, Celery, constance, allauth,
  two-factor-auth, simple-history, debug-toolbar, silk); Tailwind v4 + Alpine.js + htmx through
  Vite. Three roles (customer/distributor/admin) via Django groups with a `seed_roles` command
  (DEBUG-only). 73 constance business-rule settings seeded across 8 fieldsets
  (`apps/platform_settings/config.py`).
- **Authentication for all three roles (Tasks 4–6), with real Stitch-designed UI:** customer
  email/phone registration + password reset (Gmail SMTP verified live); distributor phone+password
  with SMS OTP via mNotify (verified end to end with a real SMS) and constance-driven lockout;
  admin login with mandatory TOTP 2FA. All flows verified in a real browser, not just pytest.
- **Catalog (Task 7):** `Category`/`Product`/`ProductImage`/`ProductVariant` models with Django
  Admin CRUD, WebP photo conversion, and a concurrency-safe stock decrement.
- **Public storefront (Task 8), with real Stitch-designed UI:** home page (featured products,
  category tiles), product listing (search/category/price filters + sort, reactive via HTMX with
  `hx-push-url` so filtered results stay shareable), and product detail page (gallery, variants,
  an honest disabled add-to-cart stub until Task 17 builds the real cart). `templates/base_store.html`
  is the first template carrying the real site header/footer/nav (`templates/base_auth.html` stays
  a header-less shell, used only by the auth screens). First template to load `main.js`
  (htmx + Alpine, bundled since Task 1 but unused until now).
- **CI:** GitHub Actions runs the full suite against a real `mysql:8` service container (local dev
  stays on SQLite — MySQL doesn't install on this Mac).
- **Binary tree + distributor onboarding (Tasks 9–10):** `BinaryTreeEdge` closure-table schema and
  `PvLedger` per-leg PV aggregates (`apps/binary_tree/`, `apps/pv_ledger/`); `BinaryTree.place_distributor`
  implements the confirmed placement/spillover algorithm (sponsor picks a leg or auto-balance falls
  back to the weaker leg; spillover stays in-leg, shallowest-first); the ancestor-aggregate read
  path is O(1)/O(log n), verified against a 16k+-node synthetic tree. Onboarding is fully
  Paystack-gated: registration fee payment creates the account (`PendingRegistration`, never
  session-only), starter pack selection sets rank/PV, and confirmed starter-pack payment triggers
  first-time tree placement plus a write-time PV credit up the ancestor chain
  (`apps/pv_ledger/services.py::record_purchase_pv`, two bulk `F()` updates grouped by leg, not a
  per-ancestor loop). Verified against Section 14's Kofi/Ama example exactly. **Known
  simplification:** the registration form has no leg-choice field yet, so placement always runs
  auto-balance — see the `project_binary_tree_placement_spillover_rule` memory before adding
  sponsor-driven leg choice.
- **Distributor KYC verification (Task 11), a Boundary-tier external integration:** replaces an
  earlier self-hosted upload form (shipped, then removed) with **Didit**'s hosted verification flow
  — distributor is redirected to Didit's page (ID front/back + a live selfie, face-match +
  liveness + document checks all run on Didit's side), redirected back via callback + HMAC-verified
  webhook (`apps/distributors/didit.py`, mirrors `paystack.py`'s shape), and the authoritative
  result is always re-fetched server-side (never trusted from the callback/webhook payload alone).
  Admin sees the result plus locally-stored images in Django Admin and approves/rejects via bulk
  actions (reject requires a reason, Django's intermediate-confirmation-page pattern) —
  **Didit's result is informational only; it never sets `kyc_status` itself**, per `SPEC.md`'s
  "never auto-approve KYC" boundary. Approval assigns a permanent IR ID via a
  concurrency-safe sequence (`IrIdSequence`, pre-seeded by a data migration, not lazily created)
  whose design went through a full `doubt-driven-development` cycle (10 findings from a fresh
  adversarial review, 8 folded in) before implementation. **Update 2026-07-14:** both assumptions
  flagged above as unverified are now confirmed — the webhook signature scheme against Didit's real
  primary docs (`X-Signature-V2`, not the originally-shipped and incorrect `X-Signature-Simple`),
  and the decision-response image-field names (`front_image`/`back_image`/`portrait_image`) against
  a real, live verification session run through a real Didit account. That same live test also
  caught and led to fixing a genuine bug the test suite alone couldn't have: the SSRF allowlist in
  `apps/distributors/services.py::_assert_safe_media_url` only permitted `*.didit.me` hosts, but
  Didit actually serves images from a specific S3 bucket, so every image was silently failing to
  download. A 12-skill retrospective pass (2026-07-13/14, covering every `agent-skills` skill never
  invoked during the original Task 11 build) also added Celery-deferred webhook processing (Didit's
  5s timeout), a `django-simple-history` audit trail for KYC/IR ID changes, and an ADR
  (`docs/decisions/0001-didit-for-kyc-verification.md`).
- **Direct Referral Bonus (Task 12), the first Commission Engine task (elevated test rigor):** two
  new apps from scratch, `apps/wallet` (`Wallet`/`WalletTransaction`, `credit()` — a lazily-created
  wallet, atomic `F()` balance update, `retry_on_lock_contention`, proven concurrency-safe with a
  5-thread test) and `apps/commissions` (`calculate_direct_referral_bonus`, establishing this
  codebase's first money-rounding convention — `Decimal.quantize(..., ROUND_HALF_UP)` to the
  pesewa). Hooked into `consume_paid_starter_pack` inside the same locked/idempotent block as
  placement and PV credit, so a webhook/callback race can't double-credit any more than it can
  double-place. **Formula confirmed by the user, not read directly from spec:** the original
  requirements doc's Section 14 walkthrough contradicts itself between its own two worked examples
  (Ama's GHS 200 vs. Kofi's GHS 75 don't reconcile under one consistent "rate × PV" or "rate ×
  price" formula) and also contradicts this project's own prior planning note for the same
  scenario — resolved as rate × PV (1 PV = GHS 1): Pack A = GHS 50, Pack B = GHS 100; see the
  `project_direct_referral_bonus_formula` memory. Sponsor gets an SMS notification (amount +
  referred distributor's name), wrapped so a notification failure can never roll back the
  already-committed credit.
- **Binary Bonus (Task 13):** the per-distributor money math (weak-leg detection, weekly cap,
  180-day carry-forward, 100-PV eligibility) shipped and was verified against real MySQL CI first;
  the remaining piece — `apps/commissions/tasks.py::calculate_binary_bonus`, the Celery Beat batch
  driver that actually runs it every `BINARY_BONUS_INTERVAL_MINUTES` for every distributor with
  pending PV — landed 2026-07-21 after a `doubt-driven-development` cycle (fresh adversarial
  review before implementation) and a `code-review-and-quality` pass after, both of which changed
  the design: a per-iteration-renewed Redis lock (not just a one-shot timeout) preventing
  overlapping cycles from minting two different `run_at` values, per-distributor exception
  isolation covering the id-to-row lookup itself (not just the payout call), a systemic-failure
  guard that raises instead of silently returning an all-zero summary if every distributor failed,
  and a self-healing sync of the live `BINARY_BONUS_INTERVAL_MINUTES` constance setting into
  `django_celery_beat`'s schedule (that setting would otherwise be purely decorative, unlike its
  neighbors in the same admin fieldset) that explicitly leaves a crontab/solar/clocked-scheduled
  task alone if an admin has repointed it via `django_celery_beat`'s own admin. Migration:
  `apps/commissions/migrations/0001_seed_binary_bonus_periodic_task.py`. **Not done:** a dedicated
  scale test asserting the batch driver's own query count stays flat at 10k+ distributors (see
  `tasks/todo.md` Task 13's Verification section) — the per-distributor function was already proven
  O(1)/O(log n) at 16k+ tree nodes in Task 9/10, and the driver's query shape is flat by
  construction, but that's reasoning, not a measurement.
- **Matching Bonus (Task 14), completing the Commission Engine — sums each distributor's downline
  binary-bonus earnings over the SPONSOR chain** (`Distributor.sponsor`, the recruitment lineage —
  deliberately not `apps.binary_tree`'s placement tree, which diverges from it under spillover),
  3 levels deep for Bronze / unlimited for Silver (hard-capped at `MAX_MATCHING_BONUS_WALK_DEPTH`
  regardless), over a rolling window set by `MATCHING_BONUS_INTERVAL_DAYS` (7 days by default,
  admin-editable — the window tracks this live, not a hardcoded 7), crediting `MATCHING_BONUS_RATE`%.
  Built 2026-07-22 via
  the full skills workflow up front (`spec-driven-development` to resolve the original task
  description's real architectural gaps, `planning-and-task-breakdown`, TDD per slice,
  `doubt-driven-development` before the batch driver, parallel `security-and-hardening` +
  `code-review-and-quality`, `code-simplification`) rather than reactively after a CI incident, the
  way Task 13's hardening was. A fresh-context `doubt-driven-development` review caught a bug before
  any code shipped: the planned batch driver would have called
  `process_matching_bonus_for_distributor` with an unsaved `Distributor(pk=id)` stub (mirroring
  Binary Bonus's own convention) — but unlike Binary Bonus's function, this one needs `.rank`, and
  the draft discarded its own locked-row fetch and read `.rank` off the stub instead, which always
  resolves to `""` — silently paying nobody, ever, with zero exceptions. Fixed before implementation
  (`apps/commissions/services.py::process_matching_bonus_for_distributor` uses `locked_distributor`
  throughout), with a regression test proving it. The audit-trail model from Task 13
  (`BinaryBonusCycleRun`/`Failure`) was generalized rather than duplicated — renamed to
  `CommissionCycleRun`/`Failure` with a `job_name` discriminator — and `apps/commissions/tasks.py`
  now shares `_run_commission_cycle`/`_sync_periodic_task_interval`/`_persist_cycle_audit_record`
  between both bonus tasks (confirmed a pure, behavior-preserving refactor for
  `calculate_binary_bonus` — its test suite needed only mechanical renames). The parallel review
  pass found one real Medium-severity gap: Silver's unlimited depth combined with the lock only
  renewing between distributors (never mid-walk) meant a pathologically deep sponsor chain could
  theoretically outlast the lock, and unlike Binary Bonus, Matching Bonus has no weekly cap to bound
  the resulting risk — fixed via the walk-depth ceiling, a `db_index` on `Distributor.rank`, and an
  `Exists()`-based pre-filter query. **Deferred, not decided silently:** a per-distributor-per-cycle
  cap (mirroring `WEEKLY_BINARY_BONUS_CAP`) would bound the blast radius of any future double-payment
  bug — flagged as new scope, same as Task 13h's deferred circuit-breaker item, not built without a
  decision. Migrations: `apps/commissions/migrations/0003_generalize_cycle_audit_models.py`,
  `0004_seed_matching_bonus_periodic_task.py`. Verified against real MySQL in CI via PR #2, which
  also went through a CodeRabbit review — that pass caught a genuine correctness bug (the downline
  earnings window was hardcoded to 7 days independent of the admin-editable
  `MATCHING_BONUS_INTERVAL_DAYS` cadence it's meant to track; an admin lowering the interval below 7
  would have caused consecutive cycles to double-count the same earnings with no cap to absorb it,
  raising it would have silently skipped earnings past the stale window) plus a docstring that
  overclaimed matching bonus's overlap-safety (it has *less* double-pay protection than Binary
  Bonus, not more — the cache lock is its sole defense, not a backstop). Both fixed pre-merge.
- **Wallet ledger (Task 15):** `apps/wallet/services.py::debit()`, symmetric to the pre-existing
  `credit()` (same validation shape, same race-free atomic conditional `.update()`, same no-self-retry
  docstring contract), storing a *negative* `WalletTransaction.amount` so `Wallet.balance` always
  literally equals `SUM(WalletTransaction.amount)` for that wallet -- no caller of `debit()` exists
  yet (Tasks 16/19 will be the first). `Wallet` gained a `CheckConstraint(balance__gte=0)`, mirroring
  `PvDailyBucket`'s established defense-in-depth reasoning. `WalletTransactionInline` and `WalletAdmin`
  itself both now hard-lock `has_add/change/delete_permission` to `False` (not just `readonly_fields`),
  mirroring `CommissionCycleRunAdmin` from Task 13 completely -- a `security-and-hardening` pass
  caught that only the inline had been locked down initially, leaving `WalletAdmin`'s own default
  delete action able to cascade-delete an entire distributor's ledger. Also built: `earnings_history`
  (`apps/distributors/views.py`), a distributor's own paginated wallet ledger page, unconditionally
  scoped to `request.user.distributor` (no id/param IDOR surface), guarded by
  `apps.accounts.permissions.is_distributor` (previously dead code, now wired in) so an authenticated
  non-distributor gets a clean 403 instead of a 500. Its UI came from a Stitch screen ("Earnings
  History - Bancostore Distributor", project `14456046746368120137`) that was fetched and verified
  against the original design prompt before building -- verification caught two real gaps (no true
  desktop icon-only sidebar collapse, only a mobile drawer; no empty-state design), both built by
  hand. `templates/distributors/base_dashboard.html` is the first shared, collapsible sidebar app
  shell in this codebase (Alpine.js, collapse state persisted to `localStorage`), and
  `templates/distributors/dashboard.html` (Task 20's placeholder) was migrated onto it so the sidebar
  doesn't disappear when navigating between Dashboard and Earnings History. See `tasks/todo.md`
  Task 15's 15a-15f breakdown for the full build/review history, including the pagination
  stable-ordering bug (`order_by("-created_at")` with no tie-breaker could skip/duplicate a row
  across a page boundary on a timestamp collision, fixed by adding `"-pk"`) caught by
  `code-review-and-quality`. Taken through the branch → PR → CI (real MySQL) → CodeRabbit → merge
  workflow via PR #4, merged 2026-07-22. Three follow-up PRs merged same day: #5 (sidebar collapse
  fix + Stitch-parity visual polish), #6 (favicon/logo/header fixes + Withdraw Now CTA + collapsed-
  icon popovers), and #7 (restored the "Last withdrawal" caption, and resolved `SPEC.md`'s Open
  Question #5 -- `MIN_WITHDRAWAL_AMOUNT`/`MAX_WITHDRAWAL_AMOUNT`/`WITHDRAWAL_DAY` seeded as GHS
  100/GHS 10,000/Friday, all still admin-editable constance settings -- unblocking Task 16).
  **Deferred, not silently skipped:** the same unguarded `request.user.distributor` pattern this
  task fixed in `earnings_history` still exists in `select_starter_pack`, `start_kyc_verification`,
  and `dashboard` (pre-existing, untouched here) -- both review passes suggested a shared
  `@distributor_required` decorator applied codebase-wide as a fast-follow.
- **Withdrawal request flow (Task 16), closing Phase 5:** built as 8 vertically-sliced sub-tasks
  (16a-16h) per `docs/decisions/0004-withdrawal-payout-design.md`, which resolved four money-safety
  design gaps up front (payout destination storage, what `WITHDRAWAL_DAY` actually gates, exact
  debit timing relative to admin approval vs. confirmed Paystack payout, and the never-wired
  `AUTO_APPROVE_WITHDRAWALS_ENABLED` settings staying permanently unwired per `SPEC.md`'s hard
  Boundary against auto-approving a withdrawal). Distributor payout destination (mobile money
  number + network) is a one-time `Distributor` profile field gating request submission the same
  way `kyc_status` already does; a distributor can request any day but only once per
  `WITHDRAWAL_FREQUENCY`; `WITHDRAWAL_DAY` gates a Friday Celery batch that actually pays out, not
  submission; the wallet is debited at admin approval (`apps/wallet/services.py::debit()`, Task
  15's previously-uncalled function), with a `credit()`-based reversal if the subsequent Paystack
  Transfer fails or is reversed -- idempotent against webhook/poll retries and duplicate webhook
  deliveries, verified by a concurrent-applications test. `apps/withdrawal/services.py` implements
  the full state machine (`submitted` -> `approved_debited`/`rejected` -> `queued_for_payout` ->
  `paid`/`reversed`), tax computed from the admin-editable `WITHHOLDING_TAX_RATE` and matching the
  doc's own worked example exactly (GHS 500 -> GHS 5 tax -> GHS 495 net). The Friday payout batch
  (`apps/withdrawal/tasks.py`) reuses Task 13/14's `CommissionCycleRun`/`Failure` audit-trail
  pattern and per-iteration-renewed Redis lock convention rather than inventing a new one; the
  Paystack Transfer wrapper (`apps/distributors/paystack.py` additions) tries `verify_transfer`
  before `initiate_transfer` on every resume so a retry landing after a crash never double-initiates
  a transfer Paystack already has. **Known limitation, not silently worked around:** this sandbox
  Paystack account's "Starter Business" tier blocks all real Transfers even in test mode (recipient
  creation live-verified working; initiate/verify are not) -- see
  `project_paystack_transfer_account_tier_blocked` memory; Checkpoint F's real-browser verification
  used a mocked Paystack response for the payout step instead. Distributor-facing status page
  (`templates/distributors/withdrawal_history.html`, Task 16g) plus SMS notifications (not in-app --
  the dashboard's notification bell stays intentionally disabled, "coming soon", until Task 21 wires
  up real Channels-based in-app notifications) on approval/rejection/paid/reversed, with the SMS
  send always placed after the locked state transition returns, never inside it. A UI bug sweep
  found and fixed the same two defects (missing table `min-width` inside `overflow-x-auto`, and
  inconsistent paired `font-X`/`text-X` design-token sizing) across both this new page and 5
  pre-existing `admin_portal` tables, each now carrying a comment documenting the convention so it
  doesn't get silently reintroduced. Checkpoint F (a distributor requests a withdrawal, tax is
  deducted correctly, admin approves, and a simulated Paystack payout succeeds) passed end-to-end in
  a real browser session 2026-07-24, verifying every financial figure against the database at each
  step, not just the UI.
- **Two full code-review + security-audit rounds** (2026-07-11/12) have run against Tasks 1–7, plus
  code-review + security-hardening passes (2026-07-13/14) against Tasks 9–11. All Critical/High
  findings are fixed (rate limiting, lockout/OTP race conditions, timing leaks, lock-contention DoS,
  CSRF-exempt SMS send, an SSRF gap in Didit image downloads, an IR ID overflow bug caught before
  it shipped). The deliberately deferred remainder — decorative constance settings,
  production security headers, proxy-aware rate-limit keys, Paystack secret encryption,
  session-engine fallback — is tracked in the "Known issues" sections at the top of
  `tasks/todo.md`; read those before touching auth or deployment code.

Existing apps: `apps/{accounts,admin_portal,binary_tree,catalog,commissions,distributors,notifications,orders,platform_settings,pv_ledger,wallet,withdrawal}`. Shared
concurrency helper: `bancostore/concurrency.py` (`retry_on_lock_contention`,
`select_for_update_nowait_if_supported`) — use it for any counter/stock/attempt update rather than
reinventing locking. See `tasks/plan.md` and `tasks/todo.md` for the full task breakdown and
what's next.

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
python manage.py seed_roles                             # seed one stub user per role (DEBUG-only)
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

**Frontend edit-verify loop (Task 8 gotcha, hit twice before being written down here):**
editing a template's Tailwind classes has no visible effect until `npm run build` runs — Vite only
compiles the classes it sees scanning templates at build time, and Django serves whatever's already
in `static/dist/assets/`. Worse, `vite.config.js` deliberately uses stable (non-hashed) output
filenames so templates can reference `{% static 'assets/main.css' %}` directly without reading the
Vite manifest — the tradeoff is no automatic cache-busting, so a browser tab can keep serving a
stale cached `main.css` even after a real rebuild. When verifying a UI change in the browser: run
`npm run build` after any template edit that adds/removes Tailwind classes, then hard-refresh
(Cmd+Shift+R) before screenshotting — don't trust a screenshot that wasn't preceded by both.
Revisit with real cache-busting once there's a deploy pipeline (Task 24). To skip the manual
rebuild step, run `npm run dev` (Vite watch mode) instead of `npm run build` — it recompiles on
every save; a hard-refresh is still needed to see it.

**Media files need an explicit URL route that pytest can never verify.** `django.contrib.staticfiles`
auto-serves `STATIC_URL` under `runserver`, but `MEDIA_URL` (uploaded product photos) doesn't get
served automatically — `bancostore/urls.py` needs its own `if DEBUG: urlpatterns += static(...)`
block (added Task 8, was missing since Task 7 — every product photo 404d in local dev the whole
time, unnoticed). This is a real bug class with **no possible pytest coverage**: Django's test
runner always forces `DEBUG=False` regardless of `.env`, so that `if DEBUG:` block never executes
under pytest. Verify media serving by curling a real file against the live `runserver` — a green
test suite proves nothing here.

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
  (test before the code, not alongside it in the same breath — see
  `feedback_vertical_slice_build_process.md`'s 2026-07-12 entry for what happened when Task 8's
  three planned slices got built and tested together instead of checkpointed one at a time). Add
  `frontend-ui-engineering` for UI work, `security-and-hardening` for anything touching
  auth/input/external integrations, `source-driven-development` when correctness depends on a
  framework's documented behavior.
- **When a task is planned as multiple slices** (e.g. 8a/8b/8c in `tasks/todo.md`): ship and fully
  verify one slice — full suite green, real browser check, then re-read that slice's acceptance
  criteria line by line and confirm each has a test that would fail without the fix — before
  starting the next slice. A slice that renders correctly in a screenshot is not the same as a
  slice whose checklist items each have a falsifiable test.
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
