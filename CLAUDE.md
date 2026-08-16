# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project State

**Tasks 1–25 are all done. Task 21 (dashboard extras: binary tree view, earnings history
verification, carry-forward tracker, notification bell) closed 2026-07-30 and was thought to be the
last piece of the MVP scope before deployment — but auditing which already-shipped backend features
still had no real, Stitch-designed frontend (Task 22/23's admin_portal precedent set the
expectation that every admin-facing screen should look like the rest of the app, not Django Admin)
surfaced two more: Task 26 (Catalog Management) and Task 27 (Admin Dashboard), both closed
2026-07-31. Task 28 (Platform Settings admin screen), also closed 2026-07-31, replaced raw Django
Admin as the primary path for all 76 constance business-rule settings, the same reasoning applied
to its own last remaining raw-Django-Admin surface. Task 29 (Storefront About & Contact pages),
also closed 2026-07-31, is further ad-hoc work in the same vein as Task 25 — replacing 6 dead
placeholder links discovered while auditing the storefront, not part of the original numbered
plan. Task 30 (fixing tracked Known Issues — decorative constance settings, session engine, CI
hygiene), also closed 2026-07-31, was requested directly by the user rather than found during an
audit. Task 31 (storefront home page — Editorial Variant layout rebuild), closed 2026-08-04, was
likewise requested directly by the user, along with a same-day Task 32 (admin portal fixes —
category-image dropzone, admin logout redirect, two leftover native-admin links), bundled into the
same PR #60. **Task 24 (deploy to Hostinger VPS) is done — the platform is live in production.**
Split into sub-tasks 24a–24h per `SPEC.md` Boundaries (each sub-task got its own explicit
go-ahead before touching the real VPS/domain), closed 2026-08-10 with a full smoke-test purchase
run end-to-end against real production MySQL/Redis at `https://bancostore.com` (register → pay →
starter pack → tree placement → KYC → IR ID → direct referral bonus → binary bonus cycle →
withdrawal request → tax deduction), all test data cleaned up afterward. Full deploy runbook lives
in `deploy/README.md`; production quick-reference commands are in `SPEC.md`'s Commands section.
Task 33 (Distributor Team page — flat sponsor-chain downline roster) also closed 2026-08-10, found
via a dead sidebar placeholder the user asked about. Task 34 (seven legal/policy pages) and Task 35
(admin-managed Social Media Links) closed 2026-08-11, both requested directly by the user. Task 36
(post-deploy fixes — real cache-busting, footer layout, Social Links relocated into Platform
Settings, icon-picker bugs, Currency locked to GHS-only) closed 2026-08-11, found by the user
reviewing the live production deploy of Tasks 34/35. Task 37 (SEO foundations — meta/OG/Twitter
tags, robots.txt + sitemap, JSON-LD structured data, a Lighthouse page-speed audit) and Task 38
(self-hosting the remaining Stitch-generated/Google-hosted images the audit flagged as a real
production risk) both closed 2026-08-11, requested directly by the user the day after launch. Two
small follow-ups closed 2026-08-12: a real hand-vectorized brand logo replacing the placeholder
wordmark, and a Google Search Console site-verification route (needed to submit Task 37b's sitemap
to Search Console). **Phase 2 work started 2026-08-12** — `SPEC_PHASE2.md` (see `SPEC.md`'s "Out
of scope" list for the ten deferred features it covers) is now **complete as of 2026-08-16**, all
ten features shipped across Tasks 39-48, Checkpoint O signed off. Tasks 39 (Wishlist), 40
(Saved/Multiple Delivery Addresses), and 41 (Product Reviews) closed first, shipped via PR #73.
- **Task 42 (Promotional Banners)**, closed 2026-08-13 via PR #74: admin uploads a banner (image,
  link target, start/end date) from a new `apps/promotions` app; the home page shows only
  currently-active banners (`start_date <= now <= end_date`) with no admin action needed on the end
  date itself, reusing Task 31's Editorial Variant home-page sections and the Task 7/26 image/WebP
  pipeline. A follow-up round added a search filter, themed date picker, and auto-rotating carousel
  per direct user feedback, plus 4 CodeRabbit fixes (keyboard-accessible combobox, carousel
  pause-state race, timezone-consistent tests, accessible names on linked slides).
- **Task 43 (Discount Codes)**, closed 2026-08-13: admin creates fixed-amount/percentage-off codes
  (`apps/promotions.DiscountCode`) with expiry, an audience restriction (retail/distributor/
  everyone, enforced via the same real `is_distributor` role check every other gate in this
  codebase uses — never `Order.pv_earned`), a global `max_uses` cap, and an independent
  `limit_one_per_customer` toggle. **A `doubt-driven-development` pass before 43b (fresh-context
  `security-auditor`) found 2 Critical + 3 High gaps before any code was written**, two of which
  were real product decisions put to the user with Shopify/Stripe/Amazon's own real-world
  precedent researched first: once a payment is captured, the order is never cancelled or rolled
  back over a redemption race (`times_used` can rarely end up over `max_uses` right at the
  boundary — an accepted, deliberate design, not an unmitigated bug, proven by
  `test_consume_discount_code_can_exceed_max_uses`); and disabling/expiring a code mid-checkout
  doesn't retroactively revalidate an order already past creation. The discount amount snapshots
  onto `Order.discount_amount` at creation time exactly like price/delivery fee already do. Audience
  restriction (43c) reuses the same generic rejection message as every other rejection reason
  ("This discount code is invalid or expired") to avoid an existence oracle, matching 43b's own
  security reasoning.
- **Task 44 (Backorders)**, closed 2026-08-13: three new global constance settings
  (`BACKORDERS_ENABLED`, `BACKORDER_MESSAGE`, `OUT_OF_STOCK_BEHAVIOUR`) — store-wide only, not
  per-product, per a direct `source-driven-development` correction against the source doc (the
  original planning-time paraphrase had wrongly assumed a per-product flag). A backorder-eligible
  out-of-stock product shows Add to Cart with the configured message instead of "Out of Stock." **A
  `doubt-driven-development` pass before 44b** found live-re-checking the backorder settings at
  `confirm_order_payment` time would be asymmetrically risky (an admin disabling backorders between
  Paystack capture and a delayed webhook could wrongly cancel an already-paid order) — resolved by
  snapshotting `Order.backorders_allowed_at_checkout` once at creation, matching this codebase's
  existing price/PV/delivery-fee snapshot convention. A new `OrderItem.stock_decremented` field
  (with a `stock_decremented <= quantity` `CheckConstraint`) records the real decremented amount so
  `cancel_or_refund_order`'s restock logic never over-credits stock that was never actually removed
  for a backordered line — backfilled for every pre-existing confirmed order via a data migration,
  verified 0 mismatches against real local dev data.
- **Task 45 (Shared CSV/PDF Export Utility)**, closed 2026-08-14: `bancostore/exports.py`
  (`export_as_csv`/`export_as_pdf`), with `csv_safe_cell` (OWASP formula-injection guard) applied
  unconditionally to every cell — extracted from `distributor_directory_export`'s own original
  per-view helper, which now reuses the shared one too. Wired the pre-existing GRA withholding-tax
  screen (`withdrawal_review_export`) to it as the first real consumer. A genuine local-only
  WeasyPrint/cffi segfault (a second independent `try: import weasyprint` site corrupting internal
  C state after a first failed dlopen on this Pango-less Mac) was fixed by computing
  `WEASYPRINT_AVAILABLE` exactly once in `tests/conftest.py`, imported everywhere else.
- **Task 46 (Sales & Revenue Reporting)**, closed 2026-08-14, three sub-tasks. 46a is a real
  architecture decision (`docs/decisions/0010-reporting-architecture.md`, confirmed with the user
  before implementation): revenue/orders-per-status/delivery-fee-by-zone/best-selling-products are
  computed daily into small rollup tables by a Celery Beat job (`DailyOrderRollup`/
  `DailyProductSales`, mirroring Binary/Matching Bonus's own scheduled-job shape) rather than a live
  aggregate query per page load, since this codebase's stated hundreds-of-thousands-of-users scale
  target means a naive live scan wouldn't hold up the way Task 27's handful of dashboard cards do;
  new-vs-returning-customers and commissions-vs-revenue stay live, indexed, date-bounded queries.
  46b/46c built the `sales_revenue_report` screen (all six report types, a Day/Week/Month toggle
  re-bucketing one already-fetched series in Python, a themed date-range filter, CSV+PDF export via
  Task 45 sharing the exact same filtered params as the on-screen report). A genuine SQLite
  quirk (`Sum()` over a `DecimalField` losing its 2dp scale) was fixed at the root in
  `get_order_summary_report` via explicit `Decimal.quantize`, not patched per-template.
  `COMMISSION_TRANSACTION_TYPES` (Task 27) was relocated from `admin_portal/views.py` to
  `apps/commissions/services.py` to avoid a circular import, verified behavior-preserving against
  103 pre-existing tests.
- **Task 47 (Compliance Dashboard + Financial Overview)**, closed 2026-08-15 via PR #78, five
  sub-tasks. 47a (elevated rigor, `doubt-driven-development` first): an escrow reserve ledger
  (`EscrowLedger`/`EscrowTransaction`) crediting the admin-configured percentage (default 5%) of
  each confirmed order's NET revenue (`subtotal - discount_amount`, never `delivery_fee` — a
  discount reduces what was actually collected) at order confirmation, atomically via the same
  `F()`-based convention as `Wallet.balance`. 47b: a live retail-vs-distributor PV ratio with an
  email alert firing only on the transition from above/at-threshold to below `RETAIL_PV_MINIMUM_PERCENT`
  (never on every order while still below, avoiding an alert-storm). 47d/47e: a unified admin-facing
  audit log merging every `HistoricalRecords()`-tracked model (`Category`, `Product`, `Distributor`,
  `Order`, `WithdrawalRequest`, later `NotificationTemplate`) with `PlatformSettingChange`'s own
  bespoke settings-change log, plus an admin-editable retention period with a real scheduled cleanup
  job — closing a real gap where several sensitive admin actions (Category/Product CRUD, Platform
  Settings changes, KYC decisions) had no history tracking at all before this task. Financial
  Overview reuses Task 27's exact "real query per number" dashboard-card pattern for total revenue,
  commissions paid, withholding tax remitted, and the new escrow balance.
- **Task 48 (Notification Template Editor + Provider-Choice Settings)**, closed 2026-08-16 via PR
  #79, four sub-tasks. `NotificationTemplate` (14 seeded rows across all real notification types —
  OTP, withdrawal x4, KYC x2, binary/referral bonus, downline joined, PV expiry, order status x2)
  with admin-editable subject/body via `admin_portal`, rendered through a bespoke regex placeholder
  substitution (`apps/notifications/rendering.py`, deliberately not `str.format`/f-strings, which
  allow attribute/code access through the format spec when the template string itself comes from an
  admin) — every real send site (OTP, withdrawal status, KYC decision, bonuses, PV expiry, order
  status) migrated onto it with a dedicated test per site proving a live admin edit reaches the next
  real send. `SMS_PROVIDER`/`EMAIL_PROVIDER` are honestly locked single-choice fields (mNotify/Gmail
  SMTP stay the only wired providers), matching Task 36h's Currency-lock precedent rather than a
  fake-functional picker. `SENDER_NAME`/`SENDER_EMAIL_ADDRESS` default to blank, meaning "use the
  server's already-validated `.env` default," preserving Task 24d's startup-time email-validation
  safety net while still allowing a deliberate live admin override. Also fixed per direct user
  feedback while reviewing a live screenshot: the admin sidebar gained a scrollbar
  (`overflow-y-auto`), the Task 47e Audit Log screen's raw constance keys/model labels/field
  names/debug-style object reprs were humanized (`apps.platform_settings.config.humanize_identifier_name`,
  reused for both constance labels and diff field names), and clicking any audit log row now opens
  an Alpine.js detail modal via `json_script`-rendered per-row data, no second server round-trip.
- **Checkpoint O — Phase 2 complete**, signed off 2026-08-16: a full re-audit of all ten
  `SPEC_PHASE2.md` features' own written success criteria against the actual shipped code and tests
  (not just that each task's todo-list box was checked) found 8 of 10 fully passing outright and
  surfaced two real, worth-recording findings. First, Discount Codes' concurrency criterion
  ("exactly one succeeds") was stale prose left over from before 43b's own `doubt-driven-development`
  review reversed that exact design — corrected in `SPEC_PHASE2.md` to describe the real,
  already-user-confirmed no-rollback-on-race behavior instead of code being changed to match
  outdated wording. Second, the GRA withholding-tax export (Task 45) had never actually gained a
  PDF option despite Task 45/47's own written success criteria requiring both CSV and PDF for every
  report — a genuine gap, fixed by adding `withdrawal_review_export_pdf`, reusing the exact same
  `export_as_pdf`/queryset-sharing pattern the Sales & Revenue report already established. Also
  root-caused and fixed a real, non-flaky test bug found while re-running the full suite: `tests/
  feature/distributors/test_earnings_history.py`'s sidebar-JS regression guard had been silently
  broken since Task 36a's real Vite cache-busting migration (asserting a hardcoded pre-hash
  filename), repeatedly misdiagnosed as "known pre-existing flakiness" across Tasks 44 through 48
  without ever actually being fixed — corrected to call the real `vite_asset()` resolver directly.
  Full suite green (1748 tests passing locally as of this fix, plus the pre-existing documented
  SQLite-only threaded-concurrency-test flakiness class, confirmed unrelated by re-running in
  isolation), CI green on real MySQL for the checkpoint's own commit (PR #80).

Task 25 didn't exist in the original plan either — added
2026-07-27 after a `source-driven-development` read of the primary source doc's Section 6.4 found
no task anywhere had ever scoped a self-service order-history page for a customer or distributor,
needed before Task 20 (Dashboard core stats) links to it. Numbered 25, not inserted as 20, despite
building first: 20/21 (Phase 8) and 22-24 (Phase 9's Admin Portal / Task 24 Deploy) are already
real, in places already-shipped-code-referenced numbers — renumbering any of those would mean
rewriting history across already-built admin_portal code, so this task took the next free integer
instead and is simply sequenced earlier than its number suggests. Tasks 26 and 27 needed no such
contortion — found and built in that same order, so they simply took the next two free integers.
What exists and is verified working:

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
  **2026-07-30 addition, user-approved after a UX discussion (admin was re-entering a TOTP code on
  every single login):** "remember this device" for 7 days
  (`TWO_FACTOR_REMEMBER_COOKIE_AGE`, django-two-factor-auth's own built-in mechanism — cookie
  signing/scoping/expiry all handled by the library, confirmed by reading its source directly, not
  reinvented here). The checkbox defaults to **unchecked** (a code-review finding: the library's own
  default is checked, an opt-out 7-day 2FA skip on the highest-value account in the system) —
  `apps/accounts/views.py::AdminLoginView.get_form()` forces `initial=False` on the already-
  constructed form instance, not via a form subclass swapped into `form_list`, because
  `two_factor.views.core.LoginView.get_form()` itself unconditionally overwrites
  `self.form_list[TOKEN_STEP]` with the OTP method's own hardcoded form class on every call — and
  since `self.form_list` is one shared object for the whole process lifetime (frozen once at
  `as_view()` time), a subclass placed there gets silently discarded process-wide the first time
  any token-step form is built, a genuine library quirk found and root-caused via a doubt-driven-
  development-style investigation before landing on the correct fix. An audit log line
  (`apps/accounts/views.py::AdminLoginView.done()`) distinguishes a login that skipped the OTP
  prompt via a remembered device from one that required a fresh code, matching this codebase's
  existing convention of auditing security-relevant admin events — this is separate from, and does
  not weaken, the existing `test_2fa_requirement_cannot_be_bypassed_via_settings_toggle` guarantee
  (a brand-new device with no remember-cookie always requires a fresh code; remembering only ever
  applies to a browser that has already completed one real TOTP proof). Verified live in a real
  browser: checked the box, logged out, logged back in, confirmed the OTP step was skipped.
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
- **Dashboard core stats (Task 20), opening Phase 8:** built as four vertically-sliced sub-tasks
  (20a-20d). `templates/distributors/dashboard.html` (20a+20b, PR #39) shows wallet balance, total
  earnings, this-week's earnings, team size, left/right leg PV, rank, and monthly personal PV, plus
  the distributor's own referral link. 20c (PR #40) replaced the placeholder with the real
  Stitch-designed frontend. **Task 20d (PR #41), the first real Django Channels consumer in this
  codebase:** `WalletBalanceConsumer` pushes a distributor's own live wallet balance over a
  WebSocket (`ws/distributors/wallet/`), group membership derived only from the authenticated
  session, never client input — the auth/group-scoping pattern every later consumer (Task 21d)
  follows exactly. A `doubt-driven-development` cycle (two independent external reviews) ran before
  any consumer code was written. Verified live in a real browser: crediting a wallet from a
  separate shell process updated the dashboard with no page refresh. Also caught, via that same
  live check: `daphne` doesn't auto-serve static files in DEBUG mode the way `runserver` does, and
  Channels/WebSockets only actually route through `daphne` locally, not `runserver` — both
  documented in this file's own gotcha sections above.
- **Binary tree view, earnings-history verification, carry-forward tracker, and the notification
  bell (Task 21), closing out the dashboard/Phase 8 scope:** re-scoped 2026-07-28 via
  `source-driven-development` (the original plan's acceptance criteria for earnings history and the
  carry-forward tracker were incomplete) into four sub-tasks. **21a (PR #44):**
  `apps/binary_tree/services.py::get_downline_tree`, a flat 3-query downline fetch (never a
  recursive DB walk) rendered as a pure-CSS nested-list tree with connector lines; two follow-up UI
  fixes shipped after real user feedback (pan/zoom canvas support, then a wheel-zoom bug that
  trapped page scroll near the canvas — fixed with `@wheel.passive.false` plus a click-to-focus
  gate, since `.prevent` unconditionally calls `preventDefault()` while wheel listeners are
  passive-by-default unless explicitly opted out). **21b (PR #47):**
  `apps/pv_ledger/services.py::get_carry_forward_summary`, a dashboard stat card showing PV carried
  forward on the distributor's strong leg plus its nearest expiry date, reusing the exact same
  weak/strong-leg comparison the real Binary Bonus cycle uses so the two can never drift apart.
  **21c:** verification only — confirmed Task 15's `earnings_history` page already fully satisfies
  Section 6.3, no code changed. **21d, the notification bell (PRs #48/#49/#50/#51):** the second
  real Channels consumer, `NotificationConsumer`, notifying on exactly Section 6.6's 6 event types
  (downline joined, binary bonus credited, referral bonus paid, withdrawal approved, KYC decided,
  PV approaching expiry — deliberately not the matching bonus, a source-doc-confirmed exclusion). A
  `doubt-driven-development` pass before any code was written caught two Critical bugs in the
  original design (an unguarded DB write that could roll back an already-succeeded business
  transaction; a synchronous push inside a caller's lock instead of `transaction.on_commit`-
  deferred). The UI (21d-iv) was built from 4 fetched Stitch screens, reconciled against real scope
  (dropped fabricated dashboard chrome, illustrations, and a mobile-only settings-icon/"Refresh
  Portal"/category-filter-chips that don't correspond to any real feature). Live-browser
  verification caught three real bugs no test suite would have: a multi-line Django `{# #}` comment
  rendering as literal page text (the same recurring footgun as Task 18f, twice in this task
  alone — see "Frontend edit-verify loop" gotcha below); `backdrop-blur-sm` (a CSS
  `backdrop-filter`) on the dashboard header silently establishing a new containing block for the
  mobile full-screen overlay's `position: fixed`, trapping it inside the header's own 64px height;
  and a two-layer live-badge failure — `channels-redis` 4.3.0 doesn't tolerate `redis-py` 8.x's
  asyncio internals (a routine idle-timeout crashed the WebSocket connection with close code 1011,
  confirmed to also silently affect the already-shipped Task 20d wallet-balance push, fixed with a
  user-approved `redis<5` pin in `requirements.txt`), layered under a stale cached DOM reference
  that survived past htmx's own `hx-swap-oob` replacing that same node. A code-review pass caught a
  fourth, 100%-reproducible bug pre-merge: `@_redirect_if_cooling_off_cancelled` on the htmx-loaded
  dropdown view swapped an entire page into the small panel for a cancelled-but-still-logged-in
  distributor — removed from all 4 new views, matching the withdrawal-flow views' existing
  exemption from that same decorator. A parallel security pass found zero exploitable issues.
- **Catalog Management (Task 26), giving the admin a real UI for product/category CRUD instead of
  Django Admin:** not in the original plan — found by auditing which already-shipped backend
  features (Task 7's `Category`/`Product`/`ProductImage`/`ProductVariant` models) still had no
  dedicated frontend, the same audit that turned up Task 27 below. Built from 5 fetched Stitch
  screens (Category List, Add Category Modal, Add Product, Delete Confirmation, Product List).
  Category and product list pages use real-time htmx auto-filter search (no Filter button,
  matching `distributor_directory`/`order_management_queue`'s existing pattern) and themed
  Alpine.js listbox dropdowns for category/status/featured filters, never a native `<select>`
  (this codebase's established reason: a native select's options popup can't be restyled via CSS in
  any browser). Product images use a dynamically-sized inline formset
  (`build_product_image_formset`, `MAX_PRODUCT_IMAGES = 5`, `validate_max=True`) and a new
  `apps/catalog/services.py::normalize_primary_image()` guaranteeing exactly one
  `ProductImage.is_primary=True` per product after any save. Several real, live-browser-caught bugs
  were fixed along the way: a CSS Grid bug where the "primary image spans both columns" treatment
  silently never worked for any tile, because `col-span-2`/`aspect-video` were toggled on a
  non-grid-item child div instead of the actual grid item (`getComputedStyle()` showed the class
  correctly applied with zero layout effect); a stacking-context bug where the hover-reveal delete
  overlay (no explicit z-index) painted over the star/primary-toggle button, blocking clicks; and
  the reveal-on-demand image tiles' add-tile not hiding once a row filled. CodeRabbit caught three
  more on PR #53: an unhandled `ValueError` from a non-numeric `?category=` querystring value
  (fixed with a `category_id.isdigit()` guard); an unhandled `ProtectedError` deleting a product
  still referenced by an `OrderItem`; and the 5-image-cap formset's `non_form_errors` never being
  rendered, so `validate_max=True` silently rejected an over-cap submission with no visible
  explanation. Also fixed, per direct user feedback: every delete icon switched from the dim
  `admin-error` red to the vibrant `primary` orange, and the admin surface's `admin-primary` maroon
  accent color (originally a deliberate "distinct admin accent," later user-rejected after seeing
  it live) was fully retired back to the same `primary` orange used everywhere else in the app —
  including fixing two dead sidebar/header logo links found during that sweep. Shipped via PR #53,
  merged 2026-07-31; `tests/feature/admin_portal/test_catalog_management.py` and
  `tests/feature/catalog/test_primary_image_normalization.py`.
- **Admin Dashboard (Task 27), replacing the Task 22 placeholder ("you're logged in, KYC Review is
  the only real feature so far") now that every other admin_portal section had actually shipped:**
  built from a fetched Stitch screen ("Admin Dashboard - Bancostore Portal"), reconciled against
  real scope before implementation — the mockup's global search bar, notification bell, settings
  gear, floating action button, "System Status" pill, and 7/30-day toggle were all dropped since
  none correspond to a feature that exists in this codebase (the same "reconcile against real
  scope" pass every other Stitch-sourced page here has gone through). Every number on the page is a
  real query: four action-needed cards (Pending KYC, Pending Withdrawals, Orders Awaiting Action,
  Low Stock Products) link straight to their own admin_portal queue and are visually accented only
  when there's something to act on; a Business Snapshot row (Total Distributors + new-this-week,
  Total Products, This Week's Orders count + GHS value, This Week's Commissions) uses a rolling
  7-day window, with module-level constants (`LOW_STOCK_THRESHOLD`, `ORDERS_AWAITING_ACTION_STATUSES`,
  `COMMISSION_TRANSACTION_TYPES`) documenting exactly which statuses/transaction types count and
  why — e.g. orders-awaiting-action deliberately excludes `pending` (unpaid, nothing to act on yet)
  and `dispatched` (in transit, waiting on the courier, not an admin); the commissions figure sums
  only binary/matching/direct-referral wallet transactions, never withdrawal debits or cooling-off
  refunds; a Recent Orders table (latest 6) reuses the existing `_order_status_pill` partial.
  CodeRabbit's review on PR #54 caught two real, fixed gaps: the Low Stock Products card linked to
  the full active product list rather than the actual low-stock subset (unlike the other three
  cards, which each land pre-filtered on their own matching queue) — fixed by adding real
  `low_stock=1` query-param support to `_filtered_products`/`catalog_product_list`, with the
  amber/red stock-badge cutoff in `product_results.html` (previously an independently hardcoded
  `10`) now reading the same `LOW_STOCK_THRESHOLD` constant so the two can never drift apart; and
  the empty-state message on a zero-result low-stock view didn't account for that new filter. 16
  tests cover every count's precise inclusion/exclusion logic (including the 7-day window boundary)
  and the orders count-vs-value distinction. Shipped via PR #54, merged 2026-07-31; full suite
  green throughout (1177 passed, 1 skipped).
- **Platform Settings admin screen (Task 28), replacing raw Django Admin as the primary path for**
  **all 76 django-constance business-rule settings** (across 10 fieldset groups) — matching every
  other admin_portal section's own "should look like the rest of the app" precedent from Task
  22/23. Reuses `apps.platform_settings.admin.BancostoreConstanceForm` directly rather than
  duplicating its field types/bounds/cross-field validation; every setting name is shown as a
  human-readable label (e.g. "Distributor Login Method" instead of `DISTRIBUTOR_LOGIN_METHOD`,
  with acronyms cased correctly — OTP, KYC, 2FA, IR ID, PV, WhatsApp). All 10 groups live on one
  page via vertical tabs — sticky with icons from `lg`/1024px up, a horizontal icon-less scroll
  strip below that, per explicit user request. Two real bugs were found and fixed via live-browser
  round-trip testing before merge: `ADMIN_2FA_ENABLED`'s disable-protection check ran *after*
  `form.is_valid()`/`form.save()`, so a real save could silently flip this mandatory setting to
  `False` (fixed the ordering, added a regression test simulating a real browser's disabled-
  checkbox submission with the key omitted entirely); and a CSS cascade bug where tab icons never
  actually hid on the mobile/tablet horizontal strip, because Google's Material Symbols stylesheet
  sets an unlayered `display: inline-block` that always beats a layered Tailwind utility regardless
  of viewport — fixed by moving the show/hide class to a wrapping element, and the same latent bug
  was found and fixed in `templates/orders/order_history.html`'s chevron icon (a separate,
  already-shipped page). CodeRabbit's review on PR #55 caught one Major, real accessibility bug
  (fixed): the checkbox toggles' real `<input>` is `sr-only` and the visual toggle track had no
  `peer-focus-visible` styling, so a keyboard user tabbing to a toggle saw no focus indicator
  anywhere on screen. Also fixed: ARIA tab semantics (`role="tablist"/"tab"/"tabpanel"`,
  `aria-selected`, `aria-controls`/`aria-labelledby`) so screen readers announce the vertical tabs
  as a real tab list; and a real functional gap where `_valid_post_data()`'s test helper built every
  field's payload from `CONSTANCE_CONFIG`'s seeded defaults rather than the live stored values —
  since `ConstanceForm.save()` writes every submitted field together, a test saving one setting
  could have silently reset any OTHER already-customized setting back to its default (fixed to
  source one `get_values()` snapshot for both field values and the version hash, with a regression
  test proving an unrelated save doesn't clobber a pre-existing custom value). **Deferred, not
  silently skipped:** CodeRabbit's suggestion to add full roving-`tabindex` keyboard navigation
  (Left/Right/Home/End arrow-key handling for the tab list, beyond the ARIA semantics already
  added) was not implemented in this PR. 12 tests in
  `tests/feature/admin_portal/test_platform_settings.py` cover permissions, every field group
  rendering, save round-trips for boolean/decimal settings, cross-field withdrawal-amount and
  percentage-bound validation, label humanization, and the `ADMIN_2FA_ENABLED` tamper-resistance
  regression. Shipped via PR #55, merged 2026-07-31; full suite green throughout (1205 passed, 1
  skipped).
- **Storefront About & Contact pages (Task 29), replacing 6 dead "Coming soon" `href="#"` links**
  in `templates/base_store.html` (desktop nav, mobile nav, footer): a new `apps/pages/` app whose
  About content is grounded directly in `SPEC.md`'s Objective section (no invented company history)
  and whose Contact page reads the already-seeded-but-previously-unused `CONTACT_PHONE_NUMBER`/
  `CONTACT_EMAIL_ADDRESS`/`WHATSAPP_SUPPORT_NUMBER`/`PHYSICAL_ADDRESS` constance settings, rendering
  only the channels an admin has actually set (a `_stripped_or_none` helper guards against a
  whitespace-only value being wrongly treated as "set"). The contact form sends real mail via the
  already-configured Gmail SMTP backend, rate-limited 5/hour per IP matching
  `apps/distributors/views.py::register`'s own `@ratelimit` convention; both `name`/`subject`
  fields reject CRLF header injection at form-validation time. `templates/base_store.html` also
  gained its first Django-messages flash component (ported from `admin_portal/base_dashboard.html`'s,
  retoned to the storefront's own `border-error/20` token), since no storefront view had ever needed
  one before. A code-review pass fixed 3 issues (WhatsApp link only stripped `+`/space instead of
  all non-digits; `message` had no `max_length` unlike its siblings; the fallback-to-
  `DEFAULT_FROM_EMAIL` path had no logging); a follow-up `security-and-hardening` pass (a
  fresh-context security-auditor agent) found 2 more Low fixes (missing `max_length=254` on
  `email`; a stale comment overclaiming `name` reaches an email header when it currently only
  reaches the body) plus one Medium, deliberately deferred rather than fixed now: `@ratelimit(key=
  "ip", ...)` reads `REMOTE_ADDR` directly with no `RATELIMIT_IP_META_KEY` configured, so once Task
  24 puts a reverse proxy in front of Django in production, every visitor's IP collapses to the
  proxy's own address and the 5/hour cap becomes site-wide instead of per-visitor — the same
  proxy-aware-rate-limit-key gap already tracked in `tasks/todo.md`'s Known Issues, flagged here
  specifically because this endpoint's blast radius (a shared Gmail SMTP sending quota, also used
  by registration/password-reset transactional email) makes it worth resolving as part of Task 24's
  own deploy checklist rather than assumed-covered by the generic entry. Live-browser-verified at
  1440px and 500px (desktop nav, mobile hamburger menu, form submission + success flash message,
  empty-state contact-channel display). Shipped via PR #56, merged 2026-07-31; full suite green for
  every file this task touched, 1219 passed as of merge (two unrelated full-suite runs surfaced a
  handful of non-reproducing SQLite `"database table is locked"` failures — a different set of
  files each time — matching this project's already-documented test-order flakiness, confirmed via
  `git stash` isolation to be unrelated to this task's code).
- **Fixing tracked Known Issues — decorative constance settings, session engine, CI hygiene
  (Task 30), per explicit user go-ahead:** 6 independent vertical slices. **30a:** two new custom
  password validators (`apps/accounts/validators.py`) read `constance.config` live inside
  `validate()`/`get_help_text()` rather than at Django's process-start `AUTH_PASSWORD_VALIDATORS`
  evaluation, making `MIN_PASSWORD_LENGTH` actually enforced for the first time and building real
  enforcement for `PASSWORD_COMPLEXITY_ENABLED` (which previously had no mechanism at all, not just
  no wiring) — both floor-clamped/fail-closed and gracefully degrade to a safe hardcoded default
  with a logged warning if constance's Redis cache backend is unreachable, rather than crashing
  every password-set path in the app. **30b:** `apps/accounts/middleware.py::SessionTimeoutMiddleware`
  makes `SESSION_TIMEOUT_MINUTES`/`ADMIN_SESSION_TIMEOUT_MINUTES` real (previously every session,
  admin included, used Django's hardcoded 2-week default) — a code-review finding caught that
  calling `set_expiry()` on every single request would force a new DB write (combined with 30e's
  `cached_db` engine) on the hottest path in a system scoped for hundreds of thousands of users,
  fixed by only renewing once the session's remaining age drifts outside `[target/2, target]`.
  **30c:** `apps/accounts/context_processors.py::google_login_flags` makes
  `GOOGLE_LOGIN_CUSTOMERS_ENABLED` real by gating the existing allauth `{% get_providers %}` block
  in `templates/account/login.html`/`signup.html`, AND'd with the pre-existing `SocialApp`-exists
  check (a flag alone can never satisfy that real DB dependency). **30d:** honest, user-confirmed
  documentation instead of new feature builds for the 3 settings NOT wired this round —
  `ADMIN_2FA_METHOD`'s actively-wrong `"sms"` default corrected to `"authenticator_app"` (no SMS
  2FA delivery path exists anywhere in this codebase), `PASSWORD_RESET_EXPIRY_MINUTES` (customer
  email-reset-link expiry would need a custom token generator, deferred to its own
  `doubt-driven-development`-reviewed task) and `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED` (distributor
  login has zero Google markup; building it is a new feature, not a wiring fix) both now say so
  plainly in their fieldset help text. **30e:** `SESSION_ENGINE` switched from `cache` to
  `cached_db` so a Redis eviction/restart no longer logs out every user platform-wide, including
  admin's mandatory-2FA state (no migration needed, `django.contrib.sessions` already installed) —
  this surfaced a real, pre-existing bug in two `test_distributor_auth.py` tests that poked a raw
  `PhoneNumber` object directly into the session (only ever "worked" because the old `cache` engine
  never actually JSON-serializes, unlike `cached_db`), root-caused via `debugging-and-error-recovery`
  and fixed to match how the real views already store it (`str(...)`) — confirmed via a full
  session-write-site audit that production itself was never affected. **30f:** `.github/workflows/
  ci.yml` gained an explicit `permissions: contents: read` block, both GitHub Actions pinned to a
  commit SHA (verified against GitHub's own API, not guessed) instead of a mutable version tag, and
  a `pip-audit` step — deliberately non-blocking (`|| true`) since this is the first vulnerability
  scan ever run against this dependency set and it surfaced a real backlog across several
  deliberately-pinned packages (e.g. `cbor2`) that need their own dedicated triage pass, now tracked
  as a new Known Issue rather than silently gated on or ignored. A `doubt-driven-development` pass
  (fresh-context security-auditor) on the 30a/30b design before any code was written found 2 High
  findings, both resolved pre-implementation: an unnecessary `SESSION_SAVE_EVERY_REQUEST` setting
  was dropped entirely (`set_expiry()` already marks a session modified on its own; the setting
  would have forced a Redis write on every anonymous storefront request too, not just authenticated
  traffic) and the `ABSOLUTE_MIN_PASSWORD_LENGTH` floor/graceful-degradation pattern described above
  was added in response to its Medium findings. A follow-up `code-review-and-quality` pass (fresh-
  context code-reviewer) approved with two Important fixes applied pre-merge: a stale hardcoded
  "Must be at least 8 characters" hint in two password-reset templates directly undermined 30a's
  own point and was removed (matching `signup.html`'s existing convention of showing only real
  validator errors), and the `set_expiry()`-every-request cost above. Independently re-confirmed by
  that same review agent's own full-suite run that the `PhoneNumber`/session bug (already found and
  fixed) and a separate, unrelated rate-limit test false-failure (traced to two stray `runserver`
  processes left running from earlier browser verification, sharing the same Redis instance as
  pytest — exactly this project's own already-documented interference gotcha) were both
  environmental, not defects in this diff. Shipped via PR #57; full suite green throughout.
- **Storefront home page — Editorial Variant layout rebuild (Task 31), requested directly by the
  user:** rebuilds `templates/catalog/home.html` to adopt the "Storefront Home - Editorial Variant"
  Stitch screen. Of the page's 9 sections, 5 (photo strip, brand statement, mission statement,
  Discover Bancostore bento, distributor CTA) were already structurally equivalent to the new
  design and stayed untouched; 4 had genuinely different layouts and were rebuilt: Hero (centered
  → split two-column with a real hero image), Shop By Category (uniform grid → asymmetric bento,
  first real category gets a large tile), How It Works (simple grid → staggered zigzag timeline
  with a connecting line), Featured Products (grid → horizontal-scrolling snap carousel). All four
  still render real `Category`/`Product` data from the existing `home` view — no fabricated
  category names or products. Live-browser responsive testing (500/768/1024/1280/1440px) caught
  two real overflow bugs the mockup itself never surfaced: the hero's split layout originally
  activated at `md:` (768px), but the 72px headline's single long words (e.g. "OPPORTUNITY") can't
  wrap and overflowed a ~350px column into the hero image — moved the split to `lg:` (1024px); even
  there, a 50/50 column (~420px) was still too narrow for 72px text, so the headline steps down to
  56px specifically at `lg:`, only returning to 72px at `xl:` (1280px). A follow-up
  `code-review-and-quality` pass found 3 more real Important issues, all fixed and re-verified
  live: the bento grid's fixed `md:grid-rows-2`/`md:h-[600px]` (8 cells) had a hard ceiling at 4
  categories — any real count of 5+ squashed a tile into an unsized row, fixed with
  `md:auto-rows-[288px]` so overflow rows match; the timeline's circles floated off the connecting
  line instead of sitting on it (`flex-row-reverse` reverses order, it doesn't center a child),
  fixed with an order-based layout putting the circle in the true middle slot at every step; and
  the new carousel's `.hide-scrollbar` removed the one native cue that there was more content, with
  no keyboard way to scroll it — fixed with `tabindex`/`role="group"`/an `aria-label`, arrow-key
  handling, and visible Previous/Next buttons, matching the accessibility bar
  `templates/distributors/binary_tree.html` (Task 21a) already set for this exact interaction
  class. Full suite green (112 passed for the touched test dirs) throughout.
- **Legal/policy pages (Task 34), requested directly by the user:** seven pages — Terms of Use,
  Privacy Policy, Cookie Policy, Disclaimer, Earnings & Income Disclosure, AI Disclaimer, and
  Returns/Refunds/Shipping — grouped under a new "Policies" footer column in `base_store.html`.
  Content is grounded in this platform's real, already-shipped features (three real user roles,
  Paystack payments, Didit KYC, the three commission types, GHS delivery-zone fees, the order
  lifecycle, the 7-day cooling-off refund) rather than generic boilerplate or invented company
  facts. One shared `templates/pages/legal/_base.html` shell plus a `.legal-content` CSS typography
  class (`static/src/main.css`) so each page writes plain semantic HTML instead of repeating
  Tailwind utility classes per paragraph. Returns/Refunds/Shipping is the one dynamic page: it
  renders the live `DELIVERY_FEE_KUMASI`/`DELIVERY_FEE_ACCRA`/`DELIVERY_FEE_OTHER_REGIONS`/
  `FREE_DELIVERY_THRESHOLD`/`PENDING_ORDER_AUTO_CANCEL_HOURS` constance values and wires up the
  previously-unused `REFUND_RETURN_POLICY_TEXT` constance field (seeded blank since early in the
  project, listed in the admin fieldset but never read by any view until now) as an optional
  admin-editable custom policy block. The AI Disclaimer honestly discloses that some decorative/
  marketing imagery (hero banners, lifestyle photography) may be AI-generated, while real product
  photos on listing/detail pages are not. 15 new tests in `tests/feature/pages/test_legal_pages.py`;
  full suite green; live-browser-verified at 500px and 1440px.
- **Social Media Links (Task 35), requested directly by the user:** replaces the storefront
  footer's 3 hardcoded, disabled "Coming soon" icons with a real admin-managed feature — staff add/
  edit/delete social media links (name, URL, icon, color) from a new "Social Links" screen in the
  admin portal (`apps/admin_portal`), matching Task 22/23's "every admin-facing screen should look
  like the rest of the app" precedent. `apps/pages/social_icons.py` self-hosts a curated 17-platform
  brand-icon registry (SVG path data + official hex colors fetched directly from the real Simple
  Icons package, CC0-licensed) rather than adding a new pip/npm dependency, per explicit
  user choice offered via `AskUserQuestion`. Pasting a URL auto-detects the platform
  (`detect_platform_from_url`, exact-or-subdomain hostname matching, never a substring check) via a
  small admin-gated JSON endpoint called on the URL field's blur — the admin can always override the
  detected icon manually, and the eventual save is independently re-validated regardless.
  `SocialMediaLink.platform` is a choices-constrained slug, never raw SVG text: the real icon markup
  an admin's browser ever renders always comes from the fixed server-side registry, closing off
  stored-XSS by construction rather than by escaping. Capped at `MAX_SOCIAL_MEDIA_LINKS = 20`
  (the user's original ask was "countless"/unlimited; a pre-implementation `doubt-driven-development`
  review — a fresh-context `security-auditor` agent — flagged an uncapped table as a real footgun
  degrading the public footer on every page load with no floor, folded into the design before code
  was written). The footer's link list is injected via a new context processor, cached indefinitely
  and invalidated by `post_save`/`post_delete` signals on the model — the same pattern constance's
  own Redis cache already uses for business-rule settings, at this project's stated "hundreds of
  thousands of users" scale. A real bug (icon-data-generation script producing single-domain tuples
  missing their trailing comma, `domains=("instagram.com")` instead of `domains=("instagram.com",)`
  — silently iterating over characters instead of the domain string, breaking auto-detect for 9
  platforms) was caught by `tests/unit/pages/test_social_icons.py`'s very first run, fixed, and
  re-verified live. A follow-up `code-review-and-quality` pass (fresh-context `code-reviewer` agent,
  verdict APPROVE) independently re-fetched live Simple Icons data to spot-check the embedded SVG/
  color data and found one real, non-security issue: Simple Icons permanently removed LinkedIn in
  v14.0.0 following LinkedIn's own trademark enforcement, and this project had unknowingly embedded
  a permanent copy of a mark the rights holder had removed elsewhere — put to the user directly
  (not decided unilaterally), who chose to drop LinkedIn from the curated set entirely (falls back
  to the generic "custom" icon) rather than keep it. Full design reasoning in
  `docs/decisions/0009-social-media-links-design.md`. 70 new tests (22 feature + 48 unit); full
  suite green; live-browser-verified end-to-end (auto-detect, color picker, save, footer render,
  delete via the shared confirm-modal) against a real `runserver` session with a throwaway staff
  account created and deleted for the check.
- **Admin portal fixes (Task 32), bundled into the same PR as Task 31's follow-up (PR #60),**
  closed 2026-08-05: a styled Alpine dropzone/preview widget (`CategoryImageWidget`) replacing
  Django's raw unstyled `ClearableFileInput` on the category admin form (required
  `FORM_RENDERER = "django.forms.renderers.TemplatesSetting"` plus `django.forms` in
  `INSTALLED_APPS` so a custom widget template is discoverable); admin logout switched from
  Django's raw `admin:logout` to allauth's `account_logout` so it lands on the branded admin login
  page instead of `/admin/login/`; two leftover native-admin links fixed (admin login logo, 2FA
  setup-complete CTA). Real bugs fixed along the way: a `ValueError` rendering an empty
  `ImageField`'s `.url`, a mis-wired `<label>` silently blocking the file picker after clearing an
  image, and (CodeRabbit, PR #60) the image-tile delete button being hover-only and therefore
  unreachable on touch devices.
- **Deploy to Hostinger VPS (Task 24), production — the platform is live.** Built as sub-tasks
  24a–24h (VPS access hardening, base packages, DNS cutover, production secrets/`.env` +
  `check --deploy` security fixes, app deploy, Supervisor process management, Nginx reverse proxy +
  HTTPS, smoke test + runbook), each with its own explicit user go-ahead per `SPEC.md` Boundaries.
  Closed 2026-08-10: `https://bancostore.com` verified live in a real browser, `deploy/README.md`
  written as the manual deploy runbook, `SPEC.md`'s Commands section got the verified production
  commands. One full smoke-test purchase journey (two real distributor accounts, one bootstrapped
  as the root sponsor, one that went through every real step — Paystack payment, phone OTP, Didit
  hosted KYC with a live selfie, admin approval, sequential IR ID assignment, direct referral bonus,
  a real binary bonus cycle run, a withdrawal request/approval/tax computation) ran end-to-end
  against real production MySQL/Redis, every figure checked against the database directly; all
  smoke-test data (23 rows) deleted afterward, leaving production at zero distributors. Found and
  fixed four real gaps no automated test would have caught: `PYTHONUNBUFFERED` missing from the
  Supervisor configs (buffered fake-SMS log output never reaching the log file); phone OTP
  verification only triggers at a later login, not right after registration (a documented gap, not
  a bug); the plural `/accounts/login/` (allauth, customer) vs. singular `/account/login/`
  (`two_factor`, admin) URL mix-up misleadingly surfacing as this project's own CSRF-failure page; a
  brand-new admin account with zero TOTP devices landing on a bare 403 instead of a forced-setup
  redirect (flagged for a future product decision, not silently patched). Real Paystack Transfer
  payout still not exercised — same known, accepted `project_paystack_transfer_account_tier_blocked`
  limitation as Task 16.
- **Distributor Team page (Task 33)** closed 2026-08-10 — a flat roster of a distributor's full
  sponsor-chain downline (`Distributor.sponsor`, the recruitment lineage, deliberately distinct from
  Binary Tree's placement/spillover structure), replacing a disabled sidebar placeholder that had
  sat since Task 15/21. Reused, not duplicated: the cycle-safe bulk-per-level BFS walk from Task 14's
  Matching Bonus was extracted into a new public `apps.commissions.services.walk_sponsor_chain_downline_ids`,
  capped at the existing `MAX_MATCHING_BONUS_WALK_DEPTH` ceiling. Shows full name, IR ID, rank, KYC
  status, and date joined (`Distributor.user.date_joined` — `Distributor` itself has no date field,
  a real gap in the original plan caught during implementation), paginated 20/page, IDOR-safe by
  construction (no id/param ever accepted). Personal PV column deliberately left off (user-confirmed)
  — stays on the dashboard/Binary Tree pages only.
- **Legal/policy pages (Task 34)** and **Social Media Links (Task 35)**, both closed 2026-08-11 —
  see their own entries above (built before the deploy/Task-24 sub-tasks landed in the todo but
  merged after; PR #68).
- **Post-deploy fixes (Task 36)**, closed 2026-08-11, found by the user reviewing the real production
  deploy of Tasks 34/35: real Vite content-hashed cache-busting (root cause of legal pages looking
  unstyled in production — browsers were serving pre-deploy assets from HTTP cache with nothing
  forcing revalidation), footer changed to 4 equal columns (Brand/Shop/Company/Policies) with Follow
  Us as its own full-width row, Social Links CRUD relocated from its own sidebar item into Platform
  Settings' General tab, icon-picker bugs fixed (clipped dropdown, auto-applied default icon color
  with a reset control), and Currency/Currency Symbol — after directly asking the user, since no
  code anywhere reads either setting and the Paystack merchant account is GHS-only — locked to a
  single disabled GHS-only value instead of shipping a non-functional 5-currency picker. Two
  follow-up rounds (36f, 36g/h) fixed a leftover redundant constance field, a sticky-nav layout bug,
  font-token inconsistencies, and finalized the GHS-lock decision via `AskUserQuestion`.
- **SEO foundations (Task 37) and self-hosting the remaining Stitch/Google-hosted images (Task 38)**,
  both closed 2026-08-11, requested directly by the user the day after launch. 37a: canonical/Open
  Graph/Twitter Card meta tags sitewide, a generated 1200×630 OG share image, home page's previously-
  missing title/description override fixed. 37b: `robots.txt` + `django.contrib.sitemaps` (products,
  categories via static pages, all legal pages), plus a data migration setting the real production
  `Site` domain (Django's sitemap framework silently defaulted every URL to `example.com` otherwise).
  37c: sitewide `Organization`/`WebSite` JSON-LD (reusing Task 35's real social links) and per-product
  `Product` JSON-LD, both serialized through a shared `bancostore/json_ld.py::dumps_for_script_tag()`
  escaping `<`/`>`/`&` (a CodeRabbit-caught real stored-XSS-via-`</script>` gap, fixed for both
  builders). 37d: a diagnostic-only Lighthouse audit against the live production site (home 90/91/100,
  shop 93/100/100) surfacing the `lh3.googleusercontent.com` third-party image risk (already known to
  have broken once in production) as the highest-impact finding, acted on immediately as Task 38 —
  10 remaining Stitch-generated images downloaded, converted to WebP, and self-hosted (~390KB of
  third-party requests eliminated, replaced with 264KB local), leaving zero `googleusercontent`
  references anywhere in `templates/`. Un-subsetted Material Symbols webfont and Nginx text
  compression/HTTP-2 flagged but deliberately not fixed (new-dependency and live-server-config
  decisions respectively, out of scope for a same-session fix).
- **Two small follow-ups closed 2026-08-12:** a real hand-vectorized brand logo wordmark (dark/
  light/orange variants) replacing the earlier programmatic placeholder, and a Google Search
  Console site-verification route (served the same way as `robots.txt`) needed to submit Task 37b's
  sitemap to Search Console.
- **Two full code-review + security-audit rounds** (2026-07-11/12) have run against Tasks 1–7, plus
  code-review + security-hardening passes (2026-07-13/14) against Tasks 9–11. All Critical/High
  findings are fixed (rate limiting, lockout/OTP race conditions, timing leaks, lock-contention DoS,
  CSRF-exempt SMS send, an SSRF gap in Didit image downloads, an IR ID overflow bug caught before
  it shipped). Some of the previously-deferred remainder was resolved by Task 30 above (decorative
  constance settings — partially, see Task 30d for the 3 still-honestly-undocumented rather than
  wired; session-engine fallback — fully). What's still open — production security headers,
  proxy-aware rate-limit keys, Paystack secret encryption — is tracked in the "Known issues"
  sections at the top of `tasks/todo.md`; read those before touching auth or deployment code.

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

**Any feature using Channels/WebSockets only works locally through `daphne`, not `runserver`
(Task 20d gotcha).** `python manage.py runserver` does not route through `bancostore/asgi.py`'s
websocket `URLRouter` — it's a WSGI-only dev server. A page served on `runserver`'s port 8000
whose JS opens a relative `ws://.../` connection has nothing to connect to on port 8000; the
Channels stack only actually runs behind `daphne -p 8001 bancostore.asgi:application`. To exercise
a live-update feature (e.g. the dashboard's live wallet balance, Task 20d) in a real browser
locally, load the page via `http://localhost:8001/...` (daphne), not 8000. See ADR-0008 for the
full design.

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

## Git & Review Workflow

**Batch pushes and PRs — don't open a PR or push for CodeRabbit review after every small fix.**
Commit locally after each fix as usual (free, no CI/CodeRabbit cost, keeps history granular).
Hold off pushing / opening or updating a PR until there's a meaningful batch of work ready, or the
user explicitly asks to commit/push/ship. When it's time to ship, push the whole batch as one PR
(or one update to an already-open PR) so CI and CodeRabbit run once across everything, not once per
fix — this avoids hitting CodeRabbit's rate limits and PRs piling up queued behind each other. If
CodeRabbit returns findings, fix them all together in a single follow-up commit/push, not one push
per finding. **Always tell the user before opening/pushing a batch PR** — don't decide silently that
a batch is "ready" and ship it; flag it and let them confirm timing. Established 2026-08-04 after a
session that pushed several small independent fixes (home page tweaks, an admin dropzone rebuild, a
logout fix, native-admin link fixes) as one bundled PR (#60, 5 commits total including one
follow-up for CodeRabbit's findings) rather than one PR per fix.

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
