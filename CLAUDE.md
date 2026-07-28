# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

**Tasks 1–19 and 25 are done — Task 25 (My Orders, built ahead of Phase 8) closed 2026-07-28;
Task 20 (Dashboard core stats) is next.** Task 25 didn't exist in the original plan — added
2026-07-27 after a `source-driven-development` read of the primary source doc's Section 6.4 found
no task anywhere had ever scoped a self-service order-history page for a customer or distributor,
needed before Task 20 (Dashboard core stats) links to it. Numbered 25, not inserted as 20, despite
building first: 20/21 (Phase 8) and 22-24 (Phase 9's Admin Portal / Task 24 Deploy) are already
real, in places already-shipped-code-referenced numbers — renumbering any of those would mean
rewriting history across already-built admin_portal code, so this task took the next free integer
instead and is simply sequenced earlier than its number suggests. What exists and is verified
working:

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
- **Cart + checkout + Paystack payment confirmation (Task 17), closing Phase 6:** built as six
  vertically-sliced sub-tasks (17a-17f) per `docs/decisions/0005-checkout-cart-design.md`, which
  resolved the delivery-zone fee table against the source doc's own worked example (Kumasi GHS 20 /
  Accra GHS 50 / Other Regions GHS 70, not a guess) and made the cart deliberately session-based
  with no `Cart`/`CartItem` DB model (`apps/orders/cart.py::Cart`, `{product_id: quantity}`,
  identical for guest and logged-in visitors). `Order`/`OrderItem` (17a) snapshot everything at
  creation time — price, PV, delivery fee, total — so a later change to a live `Product` or
  constance setting never retroactively changes an already-placed order; a `doubt-driven-development`
  review before the migration caught a missing PV snapshot field before it could become a live-read
  bug identical to the one the price/fee snapshot decisions already existed to prevent.
  `create_pending_order` (17c) creates the `Order` in `pending` status at checkout confirmation,
  before payment — a deliberate divergence from Task 16's `PendingRegistration` precedent, since
  Section 5.2's own status table treats "Pending" as a real, visible state from the moment checkout
  is confirmed. `confirm_order_payment` (17d) mirrors `consume_paid_starter_pack`'s idempotent
  locked-consume shape exactly: locks the `Order` row, verifies the exact pinned amount with
  Paystack, decrements stock per line item, credits PV (both ancestor *and* personal PV — a
  `doubt-driven-development` review before any code was written caught the original draft only
  crediting the former, which would have silently left every distributor permanently ineligible for
  the monthly-100-PV threshold from storefront purchases), and transitions to `confirmed`. An
  out-of-stock line item discovered at confirmation (stock is never reserved at add-to-cart, per
  ADR-0005 decision 5) transitions the order to `cancelled` instead — the customer's payment is
  already captured by that point, so it's never left `pending` to retry forever on every one of
  Paystack's webhook redeliveries — logged for manual admin refund follow-up; real Paystack Refund
  API automation is deliberately Task 18's scope, not built here (see the ADR's 2026-07-25 update).
  Wired into the existing shared `paystack_webhook` (its `charge.success` dispatch refactored from
  if/elif to a dict, exactly per that code's own comment anticipating a third prefix) and a new
  `order_payment_callback` mirroring `starter_pack_payment_callback`'s redirect-back-then-re-verify
  shape, never trusting the callback's own query string for anything beyond triggering the same
  server-side re-check the webhook does. Checkout's themed Delivery Zone field (and, on the admin
  side, the Distributor Directory's status filter) replaced a native `<select>` with the same
  Alpine.js listbox pattern already established by `payout_settings.html`'s mobile-money-network
  field — a native select's open options popup can't be restyled via CSS in any browser. That work
  surfaced a real, previously-shipped-once-before XSS: interpolating request-controlled values
  directly into an Alpine `x-data` JS string is exploitable despite Django's HTML auto-escaping
  (the browser HTML-decodes the attribute *before* Alpine evaluates it as JS) — fixed the same way
  `payout_settings` fixed it once already, constrain server-side then pass via `json_script`, never
  raw interpolation. `templates/orders/order_created.html` (17d) was rebuilt from the user's Stitch
  screens to branch on `Order.status`, dropping both mockups' fabricated automated-refund timeline
  and fake progress tracker that didn't match this project's real, manual-admin-follow-up decision.
  **CodeRabbit caught 5 real issues on PR #27, all fixed pre-merge:** the session cart was never
  cleared after a confirmed payment (a customer could immediately re-order the same items — fixed
  with a new `Cart.clear()`); one Paystack response read used bracket indexing instead of `.get()`,
  the one inconsistency with every other read in this codebase, turning a malformed response into
  an unhandled 500 after the order already existed; `order_payment_callback` reversed an
  unvalidated, fully attacker-controlled query-string reference into a URL — confirmed via
  `manage.py shell` that any reference containing `/` raises `NoReverseMatch` (the `<str:...>`
  converter excludes it), an unauthenticated 500 on a public endpoint, fixed by resolving the
  `Order` first; the confirmation template's bare `else` branch would have mislabeled a later
  lifecycle status (`processing`/`dispatched`/etc., Task 18's scope) as still awaiting payment; and
  a stale test comment. Verifying 17d's own concurrency test also surfaced and fixed a genuine
  local-only flake: a 5-thread test (mirroring `apps/wallet`'s own convention) was intermittently
  flaky under SQLite specifically because `confirm_order_payment`'s correctness depends on
  `select_for_update` actually resolving contention, which SQLite has no real implementation of at
  all — unlike the wallet test, whose correctness instead comes from a race-free bulk `F()` update.
  Reduced to 2 threads matching `test_consume_paid_starter_pack.py`'s own existing precedent for
  this exact lock shape, verified reliable across 8/8 repeated local runs. Task 17e's mobile-
  responsive browser pass ran 2026-07-26: `cart.html`/`checkout.html`/`order_created.html` verified
  clean at 1440/1024/768/500px real browser resize (500px, not 320px, being macOS Chrome's actual
  window-resize floor — the natural iframe workaround is correctly blocked by Django's own
  `X-Frame-Options: DENY`, not weakened just to enable a test); see `tasks/todo.md` Task 17e for the
  full reasoning on why 500px-clean plus no hard-coded fixed-width elements gives reasonable, if not
  literally-320px-proven, confidence. Shipped via PR #24 (17a), #25 (17b), #26 (17c), #27 (17d);
  full suite green throughout, 844 passed as of 17d's merge.
- **Order status lifecycle + admin order management (Task 18), closing out the checkpoint Task 17f
  left open ("order status updates correctly with notifications"):** built as seven vertically-
  sliced sub-tasks (18a-18g) per `docs/decisions/0006-order-lifecycle-and-admin-management-design.md`.
  `apps/orders/services.py::_ALLOWED_TRANSITIONS`/`is_legal_order_status_transition` (18a) is the
  single source of truth for the whole lifecycle graph — every later transition function checks
  against it rather than hand-duplicating the rules. `cancel_or_refund_order` (18b, a three-cycle
  `doubt-driven-development` pass before any code) reverses stock and PV for an already-paid order
  being cancelled/refunded — cancellation is pre-dispatch only, refund has no timing restriction and
  requires an explicit `restock` choice (goods physically returned vs. a pure financial refund);
  `PvLedger` reverses directly, `PvDailyBucket`/`MonthlyPersonalPv` only reverse what's still live
  (fungible pools that may already be partially consumed or expired), and an already-paid Binary/
  Matching Bonus is never clawed back — a documented, accepted limitation, not a bug.
  `advance_order_status` (18c) handles the non-money-adjacent Processing/Dispatched/Delivered
  progression. `auto_cancel_unpaid_orders` (18d) is a Celery Beat batch job auto-cancelling
  `pending` orders older than the admin-editable `PENDING_ORDER_AUTO_CANCEL_HOURS` (default 24),
  reusing Task 13/14's `CommissionCycleRun`/`Failure` audit-trail pattern, generalized to
  `OrderCycleRun`/`Failure` via a new migration after explicit user sign-off. The admin_portal
  backend (18e) — order queue filtering/pagination, cancel/refund/advance actions, and a PDF
  invoice endpoint — hit a real local-environment wall: WeasyPrint eagerly `dlopen()`s the system
  Pango library at import time, which isn't installable on this Mac (macOS 12 is an unsupported
  Homebrew Tier-3 config); verification was deferred to CI instead (a new `apt-get install
  libpango...` step, this codebase's first `pytest.mark.skipif`), which then caught a real
  `pydyf`/WeasyPrint version incompatibility the very first time that code path executed anywhere,
  fixed by pinning `pydyf`. The Stitch-designed frontend (18f) — Order Management Queue and Order
  Detail pages — replaced Task 18e's placeholder template, added the previously-missing
  `order_detail` GET view (18e shipped no single-order detail page), and, per explicit user
  follow-up requests, converted the whole filter bar to real-time htmx filtering (no Filter button,
  mirroring `distributor_directory`'s own pattern) and replaced the native date-range `<input
  type="date">` filters with a fully custom themed Alpine.js calendar popover (same "native popups
  can't be restyled via CSS" reasoning that already justified the status filter's custom listbox).
  Real-browser verification — not just pytest — caught and fixed several genuine bugs no test
  suite would have: a `position: sticky` panel visually overlapping a sibling box once its content
  grew tall enough; a multi-line Django `{# #}` comment rendering as literal visible text (Django's
  comment tag is single-line only, unlike `{% comment %}`) — this recurred three separate times
  across new templates before every instance was grepped clean; and a genuine functional bug where
  "Cancel Order" would show a success message while silently doing nothing to a still-unpaid
  (`pending`) order, because `is_legal_order_status_transition` alone doesn't know
  `cancel_or_refund_order`'s narrower real precondition. A CodeRabbit pass on the resulting PR (#33)
  found two real, fixed issues (an unquoted CSS font-family failing Stylelint, duplicate Alpine
  `x-for` keys in the date picker's weekday header) plus five nitpicks, all addressed — fixing one
  of them (removing `required` from the status-advance radios to stop a native validation bubble
  anchoring to an off-screen control) surfaced a worse regression caught before it shipped: a blank
  submission would otherwise have leaked a raw Python enum repr into the admin-facing flash message.
  Task 18g closed the arc: full suite green (945 passed, 1 skipped — the skip is the documented
  WeasyPrint/Pango CI-only test), CI green on real MySQL, CodeRabbit resolved, and the remaining
  Checkpoint G piece verified live (not just pytest) — `MNOTIFY_API_KEY` temporarily blanked in
  `.env` with explicit user sign-off (never spend real SMS credit or Paystack calls without asking)
  to exercise the real fake-sender notification path end-to-end through the actual admin UI, which
  also surfaced and fixed a real unrelated gotcha: Django's autoreloader re-execs its worker
  subprocess without inheriting the `-u` interpreter flag, so unbuffered `print` output needs
  `PYTHONUNBUFFERED=1` (an env var) instead. Shipped via PR #33; full suite green throughout, 945
  passed as of merge.
- **7-day cooling-off refund (Task 19), opening and closing Phase 7:** built as three vertically-
  sliced sub-tasks (19a-19c) per `docs/decisions/0007-cooling-off-refund-design.md`, which resolved
  Section 9's own real gaps against the source doc directly (`source-driven-development`) rather
  than trusting `tasks/todo.md`'s paraphrase: the 7-day window anchors to
  `Distributor.starter_pack_confirmed_at` (matching how real cooling-off consumer-protection law
  attaches the right to the purchase date, not registration), `BinaryTreeEdge` placement is never
  removed (no removal mechanism exists anywhere in this codebase, and Section 9 never asks for it)
  — the distributor is soft-deactivated instead, only the sponsor's one-time direct referral bonus
  is reversed (not any Binary/Matching bonus, mirroring ADR-0006's already-accepted "PvDailyBucket
  is a fungible pool, can't attribute precisely once mixed into a batch cycle" limitation), a
  sponsor-wallet shortfall is logged and the refund proceeds regardless, and the refund is credited
  to the distributor's own wallet (not paid out externally, since unlike a storefront customer a
  distributor already has a real `Wallet` + withdrawal flow). 19a extracted
  `apps/orders/services.py::_reverse_ancestor_pv` (Task 18b) into a shared
  `apps/pv_ledger/services.py::reverse_ancestor_pv(distributor, pv_amount, purchase_date)`, also
  fixing a real date-drift bug in `consume_paid_starter_pack` (three independent `timezone.now()`
  calls that could straddle a UTC-midnight boundary) found by a `doubt-driven-development` review
  before the extraction. 19b built `apps/distributors/cooling_off_services.py::
  cancel_membership_and_refund` — a `doubt-driven-development` review before implementation caught
  4 real defects: the sponsor's bonus reversal must use the amount actually credited (looked up
  from the original `WalletTransaction`, not recomputed from the live `DIRECT_REFERRAL_BONUS_RATE`,
  matching this codebase's snapshot-at-event-time convention everywhere else money is involved),
  the sponsor's `Wallet` row needed this codebase's NOWAIT locking convention (not a plain blocking
  `select_for_update()`), the cancelling distributor's own row and its `BinaryTreeEdge` ancestors
  needed one combined ascending-pk-sorted lock set (not the distributor's row locked separately and
  first, which could invert lock order against a concurrent cancellation), and a new
  `MembershipCancelled` guard was needed in `snapshot_starter_pack_choice` to close a double-credit
  path reachable via an admin reactivating a cancelled account and re-purchasing a starter pack. A
  follow-up `security-and-hardening` review caught a 5th: a replayed Paystack webhook for the old
  `starter_pack_payment_reference` after cancellation was only stopped incidentally (a cleared
  price field happened to fail the amount check), fixed with an explicit guard in
  `consume_paid_starter_pack`. 19c built the real distributor-facing UI from two fetched Stitch
  screens (Cancel Membership Eligible/Ineligible), reusing `payout_settings.html`'s existing
  Alpine.js confirm-modal pattern rather than the Stitch mockup's native `confirm()`/`alert()`
  calls, and verified end-to-end in a real browser (not just pytest) — wallet credit, PV reversal,
  sponsor bonus reversal, and logout all confirmed against the database. All six PV/logic fixes
  above carry RED→GREEN regression tests, verified by actually reverting each fix and confirming
  failure before restoring it.
  **CodeRabbit caught two more real, previously-unnoticed defects across the three PRs** (a real
  gap in this project's own stacked-PR workflow: PRs targeting a non-default branch get their
  CodeRabbit auto-review silently skipped entirely until retargeted to `main`, discovered only at
  merge time): `reverse_ancestor_pv` early-returned for a distributor with no ancestors (a root
  distributor) before ever reaching the `MonthlyPersonalPv` reversal that runs after the ancestor-
  leg loop — silently leaving a root distributor's own personal PV permanently inflated after any
  cancellation/refund, affecting both this task and Task 18b's order-cancellation path, fixed and
  the weak existing test (which only asserted "no exception") strengthened to actually check the
  reversal; and a real design gap this task's own ADR hadn't resolved — the refund credited to the
  distributor's wallet was unclaimable, since the same cancellation call also sets
  `user.is_active = False`, and Django blocks login entirely for an inactive user. User-confirmed
  fix (of three options presented): a cooling-off-cancelled distributor can still log in, routed
  through a new `apps/distributors/views.py::_redirect_if_cooling_off_cancelled` decorator to
  withdrawal-only views. This needed two changes to `PhoneNumberBackend`, not one —
  `authenticate()` alone wasn't enough, since `AuthenticationMiddleware` also calls `get_user()`
  (inherited from `ModelBackend`, which blanket-checks `is_active` too) on every subsequent
  request, not just at login — caught mid-implementation when a `client.login()`-based test kept
  redirecting to the login page despite `authenticate()` succeeding. Shipped via PR #35 (19a), #36
  (19b), #37 (19c), each stacked on the previous and merged into `main` in order 2026-07-27; full
  suite green throughout, 986 passed, 1 skipped as of merge.
- **My Orders — self-service order history (Task 25), built ahead of Phase 8:** a self-service
  purchase-history page for both customers and distributors (`Order.customer` is a plain FK to
  `settings.AUTH_USER_MODEL`, not `Distributor`-specific, so one view/template serves both roles
  with no branching), found by reading Section 6.4 of the primary source doc directly — no task
  anywhere in the original plan had ever scoped this page. `apps/orders/views.py::order_history`
  (paginated, `request.user`-scoped, `-created_at`/`-pk` stable ordering matching Task 15d's own
  precedent, `prefetch_related("items__product__images")` to avoid N+1, Django's elided page range
  so a customer with many orders doesn't get thousands of pagination links) and `order_detail` — a
  **new** view, deliberately not a reuse of `order_confirmation_view`: that view's "no ownership
  check, unguessable UUID token" design is safe only because it's reachable solely via a one-time
  post-checkout redirect, never a durable bookmarkable link — a `doubt-driven-development` finding
  before any code was written. `pk=pk, customer=request.user` is IDOR-safe by construction. UI
  built from two fetched Stitch screens (desktop + mobile), reconciled into one responsive template
  matching `cart.html`/`checkout.html`/`order_created.html`'s own established convention. Two real
  bugs caught via real-browser verification (not just pytest) and a follow-up code-review pass, both
  fixed with regression tests: `order_created.html` (reused for `order_detail`) only rendered the
  item/delivery-info block for `status == "confirmed"`, so a processing/dispatched/delivered/
  refunded order — most of an order's real life — showed a bare "Order Placed" message with no
  items at all; broadened to any non-"pending" status, and every one of the 7 `Order.Status` values
  got its own accurate header copy (the cancelled copy no longer assumes the cause was always the
  insufficient-stock auto-cancel path, since Task 18b's admin `cancel_or_refund_order` can cancel
  for any reason). `base_store.html`'s header had no logged-in state at all (a logged-in user saw
  the same Log In/Become a Distributor buttons as a guest) — fixed with My Orders/Log Out links,
  and a follow-up code-review pass caught that the Log Out link didn't actually work (`allauth`'s
  `LOGOUT_ON_GET` defaults to `False` and this project never overrides it), fixed with a CSRF-safe
  POST form. CodeRabbit's own pass on the pushed fix caught one more real bug: the authenticated nav
  controls were `hidden lg:flex` while the mobile hamburger menu is `md:hidden`, leaving a genuine
  gap between `md` and `lg` (roughly 768-1024px) where a logged-in user could reach neither control
  — fixed to `hidden md:flex` and verified live at 900px. Shipped via PR #38, merged into `main`
  2026-07-28; full suite green throughout, 1009 passed, 1 skipped as of merge, real MySQL CI green,
  CodeRabbit clean on the final commit.
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

**Don't run the full pytest suite in the background while live-browser-testing against
`runserver` (Task 17c gotcha).** `tests/conftest.py`'s `autouse=True` `_clear_django_cache`
fixture calls `cache.clear()` before and after every single test, and `REDIS_URL` defaults to the
same `redis://localhost:6379/0` for both pytest and the manually-running dev server — there's no
separate DB number splitting them. A background full-suite run will intermittently wipe the dev
server's session/cart data mid-browser-check, with no error surfaced anywhere, and it looks
exactly like a real "data disappears" application bug (this cost a long, disciplined debugging
session before `redis-cli monitor` + TTL polling + server-log timestamp correlation proved the
cart/checkout code was correct and the interference was the actual cause). Either run the full
suite in the foreground and wait for it before live-testing, or don't run it at all while a
`runserver` browser session is active.

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
