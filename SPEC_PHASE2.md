# Bancostore — Phase 2 Spec

## Status

Plan phase complete (`tasks/plan.md`/`tasks/todo.md` carry the full Task 39-48 breakdown, per
`agent-skills:planning-and-task-breakdown`); Implement phase in progress. **Tasks 39 (Wishlist), 40
(Saved/Multiple Delivery Addresses), and 41 (Product Reviews) are closed**, shipped via PR #73.
Tasks 42-48 (Banners, Discount Codes, Backorders, Reporting, Compliance, PDF/CSV Export,
Notification Templates) have not been started — see `tasks/plan.md` Phases 14-16.

**Relationship to `SPEC.md`:** `SPEC.md` remains authoritative for everything Phase 2 doesn't
change — tech stack, local dev environment, production deploy, scale architecture, code style,
general testing strategy, and the Boundaries that aren't superseded below. This document only
covers what's new. `SPEC.md`'s "Out of scope (defer to Phase 2 spec)" section already links here
(updated 2026-08-12, with the user's go-ahead per this project's own standing convention on editing
`SPEC.md`).

**Grounded in the primary source doc directly** (`docs/Bancostore_Features_and_Workflow_v4.docx`,
Sections 10.2/10.3, 11, 12.4–12.6, 13.5/13.6/13.7/13.8/13.10/13.11/13.12, and 4.2), read via
`python-docx` rather than paraphrased from memory — matching this project's established
`source-driven-development` convention (the same one that caught Task 25's missing order-history
page and Task 15's incomplete acceptance criteria). Cross-checked against the live
`apps/platform_settings/config.py` to confirm exactly which settings are real today vs. stubbed —
not assumed from `CLAUDE.md`'s own summary.

**MVP context:** Tasks 1–38 are done and live in production at `https://bancostore.com` (Task 24
deployed 2026-08-10). Every feature below was deliberately deferred out of MVP scope, not
forgotten — `SPEC.md`'s own "Out of scope (defer to Phase 2 spec)" list named six broad deferred
areas, which map to eight of the ten features below (two of those six bullets each split into two
separate features here: "sales/revenue/compliance reporting" became Features 5+6, and "discount
codes/promotional banners" became Features 2+3); the other two features (Wishlist, saved delivery
addresses) were found during this spec's own source-doc read as real Section 4.2 features that were
never built *and* never explicitly deferred anywhere — a genuine gap, not a decision, closed by
user confirmation (see Open Questions).

## Objective

Build the features the MVP spec deliberately deferred, now that Bancostore is live and has real
production traffic to design them against: give admins real sales/compliance visibility, give
customers a reason to come back (reviews, wishlist, saved addresses) and a reason to buy more now
(discount codes, promotional banners), close the two real operational gaps discovered live in
production or by direct user request (backorders, PDF/CSV export), and finish wiring the two
settings groups (13.8 Notification & Communication, 13.11/13.12 Discount/Compliance) that have sat
as raw admin-facing UI with no real behavior behind them since Task 28.

**Users and what "done" looks like per audience:**
- **Admin:** can see real revenue/commission/order/delivery numbers without a database query, sees
  a live compliance ratio and escrow balance instead of a spec promise, can create a discount code
  or banner without a developer, can export any report as CSV/PDF, can moderate reviews, can edit
  the wording of any outbound SMS/email template.
- **Customer:** can save a delivery address instead of retyping it every checkout, can save
  products to a wishlist, can leave a review after a delivered order, can enter a discount code at
  checkout, can still order a product that shows "Ships in 7 days" instead of hitting a dead end.
- **Distributor:** everything a customer gets, plus every existing distributor-specific feature is
  unaffected (Phase 2 adds nothing new to the commission engine itself).

## Scope: The Ten Features

Each feature below states its source-doc grounding, what already exists in the codebase that it
builds on, the real business rules (not paraphrased), and success criteria. Data model /
service-layer design is deliberately left to the Plan phase — this is a Specify-phase document.

### 1. Product Reviews (source doc Section 10.3)

> "After receiving their order, customers can leave a star rating and written review on the
> product page. Reviews are submitted to admin for approval before appearing publicly. Admin can
> approve or delete any review."

**Builds on:** `Order`/`OrderItem` (Task 17a) already know exactly which products a given customer
received and when (`Order.status == "delivered"`), so eligibility is a real query, not guesswork.
`apps/catalog` already has the `Product` detail page and admin CRUD (Task 26) to extend.

**Business rules (from the source doc + the 13.10 settings table):**
- A review requires a star rating + written text, tied to a specific delivered `Order`/`Product`
  pair — **one review per customer per product, not per order, confirmed with the user and closed**
  (a repeat buyer edits their existing review rather than stacking duplicates; enforced at the DB
  level via `Review`'s own `UniqueConstraint(user, product)`, Task 41a).
- New reviews are **not** publicly visible until an admin approves them (13.10's "Product Review
  Approval" setting: Auto-approve vs. Manual — source doc's own "Current Value" is Manual; make
  this a real admin-editable constance toggle, not hardcoded to manual forever).
- Admin can approve or delete (not just hide) any review from the existing `admin_portal` Catalog
  Management screens (Task 26), matching that app's own "every admin surface looks like the rest of
  the app" precedent — not raw Django Admin.

**Success criteria:**
- A customer with a delivered order for a product sees a "Leave a review" control on that product's
  order-detail/product page; a customer with no delivered order for that product does not.
- A submitted review does not appear on the public product page until an admin approves it.
- Product detail page shows approved reviews + average star rating.
- Admin can approve/delete from `admin_portal`, matching Task 26/27's visual/interaction pattern.

### 2. Discount Codes (source doc Section 11.1)

> "Admin creates discount codes... a fixed amount off or a percentage off... expiry date... which
> type of customer can use it: retail customers only, distributors only, or everyone... a usage
> limit... At checkout, the customer enters the code and the discount is applied immediately."

**Builds on:** `apps/orders/cart.py::Cart` (session-based, Task 17) and the checkout flow's
existing snapshot-at-creation-time convention (`Order.subtotal`/`delivery_fee`/`total` frozen at
`create_pending_order`, Task 17c) — a discount amount must be snapshotted the same way, never
re-derived from a live, possibly-since-expired code after the fact.

**Business rules, exactly as specified — no invented ones:**
- Fixed-amount or percentage-off, admin's choice per code.
- Expiry date per code.
- Audience restriction per code: retail-only, distributor-only, or everyone — `Order.pv_earned > 0`
  is this codebase's existing real signal for "was a distributor purchase," confirmed by reading
  `apps/orders/services.py` directly rather than assumed; the *customer's* role (not just this
  order) is the more natural check and should use `apps.accounts` role groups instead, consistent
  with how every other role gate in this codebase works (`is_distributor`, etc.) — a Plan-phase
  decision, not decided here.
- Usage limit: a total redemption cap across all customers, admin-set per code (the source doc
  gives no per-customer limit — worth confirming in Plan phase whether "50 times total" also
  implies a 1-per-customer default, or genuinely unlimited-per-customer up to the global cap).
- 13.11's settings table adds two more real constance settings beyond the code model itself:
  "Maximum Discount Per Order" (a platform-wide cap, separate from any single code's own amount)
  and "Discount Applicable To" (a platform-wide default, distinct from the per-code audience
  restriction above — the source doc lists both, so both are real, not redundant).

**Concurrency/money-safety note, flagged now because it's the same class of bug this codebase has
hit before (Task 18b's stock-decrement race, the wallet's `retry_on_lock_contention` pattern):** a
usage-limited code redeemed by many customers near-simultaneously needs the same atomic-counter
discipline as `Product` stock decrement, not a naive read-then-write. This needs a
`doubt-driven-development` pass before implementation, same as every other money-adjacent feature
in this codebase.

**Success criteria:**
- Admin creates a code (amount/percentage, expiry, audience, usage cap) from `admin_portal`.
- At checkout, a valid code reduces the order total by the correct amount, snapshotted onto the
  `Order` the same way price/delivery fee already are.
- An expired, exhausted, or audience-mismatched code is rejected with a clear message, not silently
  ignored or a 500.
- Two customers redeeming the last unit of a usage-limited code concurrently: exactly one succeeds
  (a real concurrency test, matching this codebase's elevated rigor standard for money code).

### 3. Promotional Banners (source doc Section 11.2)

> "Admin can upload promotional banners that appear on the home page. Banners can link to a
> specific product, category, or page. Admin can set start and end dates for each banner so it
> disappears automatically."

**Builds on:** `templates/catalog/home.html`'s existing Task 31 Editorial Variant layout (photo
strip / bento sections already exist and already render real `Category`/`Product` data — a banner
slot is a new section, not a rebuild) and the image-upload + WebP-conversion pipeline already
proven for `Category`/`Product` images (Task 7/26).

**Success criteria:**
- Admin uploads a banner (image, link target, start/end date) from `admin_portal`.
- Home page shows only currently-active banners (`start_date <= now <= end_date`), automatically —
  no admin action needed on the end date itself.
- A banner linking to a specific product/category/page navigates there correctly.

### 4. Backorders (source doc Section 10.2)

> "Admin can enable backorders — allowing customers to still order an out-of-stock product with a
> message like 'Ships in 7 days'."

**Builds on:** `apps/catalog`'s existing stock-decrement path (Task 7, concurrency-safe) and
`apps/orders/services.py::confirm_order_payment`'s existing out-of-stock handling (Task 17d: an
out-of-stock line item discovered at confirmation currently cancels the order and logs it for
manual admin refund follow-up — ADR-0005's decision 5 deliberately never reserves stock at
add-to-cart). 13.6's settings table adds "Backorders On/Off" (global default) and "Backorder
Message" (admin-editable copy); 13.10 adds a per-outcome "Out of Stock Behaviour" choice (Hide
product / Show 'Out of Stock' / allow backorders) that should live on `Product`, not just the
global toggle, since a real store rarely wants backorders available on every product uniformly.

**Real interaction with already-shipped Task 17d/18b logic, flagged now, not discovered mid-build:**
this feature changes the out-of-stock behavior `confirm_order_payment` currently has —
backorder-enabled products must NOT hit that function's existing cancel-and-refund path. This needs
its own `doubt-driven-development` review before implementation given the existing money-adjacent
code it touches, and is exactly the kind of interaction a Plan-phase task breakdown should surface
explicitly rather than patch around silently.

**Success criteria:**
- A backorder-enabled, out-of-stock product still shows Add to Cart with the admin-configured
  "Ships in N days" message, instead of "Out of Stock."
- Checkout/order confirmation for a backordered item does not decrement negative stock or crash,
  and does not trigger Task 17d's existing out-of-stock auto-cancel path.
- A non-backorder-enabled out-of-stock product's existing behavior (Task 7/17) is unchanged.

### 5. Sales and Revenue Reporting (source doc Section 12.4)

> Total platform revenue (daily/weekly/monthly); total commissions paid vs. revenue; best-selling
> products; new vs. returning customers; orders per status; delivery report by zone; export to
> CSV/PDF.

**Builds on:** every underlying number already exists somewhere real — `Order`/`OrderItem`
(revenue, best-sellers, delivery zones), `WalletTransaction` (commissions paid, Task 12's own
`transaction_type` field already distinguishes binary/matching/direct-referral, reused as-is by
Task 27's Admin Dashboard `COMMISSION_TRANSACTION_TYPES` constant), `Order.status` (order-per-status
counts, same enum Task 18a's `_ALLOWED_TRANSITIONS` already governs). Task 27's Admin Dashboard
already proved the "real query per number, module-level constants documenting inclusion/exclusion"
pattern at a smaller scale (4 action cards + a 7-day snapshot row) — this feature is that same
pattern at report scale, not a new architecture.

**Scale note, mandatory per `SPEC.md`'s own "Scale Architecture" section:** this codebase is
explicitly built for hundreds of thousands of users, and Task 27's dashboard already runs live
queries against real tables for a handful of numbers — a full historical report (e.g. "monthly
revenue for the last 12 months" or "best-selling products all-time") run as a live aggregate query
every page load will not hold at that scale the way single-number dashboard cards do. This is a
real Plan-phase design question (pre-aggregated daily rollup tables, matching the PV ledger's own
event-driven-aggregate precedent, vs. a scheduled Celery report-cache job, vs. accepting live
queries with a defined lookback cap) — not resolved in this Specify-phase document, but explicitly
flagged so it isn't silently built the naive way the way the PV ledger's own original design
document warned against once already (see `CLAUDE.md`'s "one non-obvious architectural constraint").

**Success criteria:**
- Admin sees each of the six report types (revenue, commissions paid vs. revenue, best-selling
  products, new vs. returning customers, orders per status, delivery by zone) with real numbers
  from the live database — CSV/PDF export (Feature 7) is a shared capability applied to these six,
  not a separate seventh report.
- Every report is exportable as both CSV and PDF from the same screen.
- A report scoped to a date range returns correct numbers for that range specifically (a real test,
  not just "the page renders").

### 6. Compliance Dashboard (source doc Section 12.5) + Financial Overview (12.6)

> Live retail-vs-distributor sales ratio with an alert if retail falls below 70% of monthly sales;
> full audit log; escrow reserve tracker (5% of product revenue, "held at GCB Bank"). Financial
> Overview: total revenue to date, total commissions paid, total withholding tax remitted, escrow
> balance.

**Retail/distributor ratio — builds on a real, already-established signal:** `Order.pv_earned > 0`
already distinguishes a distributor purchase from a regular one at the `Order` level (confirmed by
reading `apps/orders/services.py` directly), matching Section 1.4's "Two Types of Purchase." 13.12
adds "Retail PV Minimum (%)" (already seeded conceptually as 70% in the source doc, but not yet a
real constance setting anywhere in `apps/platform_settings/config.py` — confirmed via grep, this is
new) and "Compliance Alert Email" (the address that receives the below-threshold alert — reuses
this codebase's existing Gmail SMTP path, no new integration).

**Escrow reserve tracker — internal ledger only, per direct user confirmation (not a real GCB Bank
integration):** a running balance of 5% of confirmed product revenue (13.12's "Escrow Reserve
Percentage (%)," itself a new admin-editable setting, not hardcoded to 5%), computed and stored the
same way `Wallet.balance` already is — an atomic `F()`-based running total credited at order
confirmation, not a live `SUM()` over all historical orders on every page load. This is bookkeeping
only: Bancostore is not calling any real GCB Bank API, and this spec does not scope one — a
deliberate, user-confirmed decision (see Open Questions), matching this codebase's own precedent of
never silently attempting an external integration nobody asked for (the Paystack Transfer
account-tier block is the cautionary example already on record).

**Audit log — a real gap, not a green field:** `django-simple-history` already exists and is
already wired to `Order`, `Distributor`, and `WithdrawalRequest` (confirmed via grep — those are
the only three models with `HistoricalRecords()` today), and `AdminLoginView.done()` already logs a
line distinguishing a fresh-OTP login from a remembered-device one. But there is no single,
admin-facing screen surfacing any of this across models, and several sensitive admin actions have
no history tracking at all yet (Product/Category CRUD from Task 26, Platform Settings changes from
Task 28, KYC approve/reject decisions). Section 12.5's "full audit log: every action... recorded
with a timestamp" needs: (a) `HistoricalRecords()` added to the models that are missing it and are
worth tracking, (b) one real admin_portal screen that queries across them, and (c) 13.12's "Audit
Log Retention Period (days)" wired as a real scheduled cleanup, not decorative — this is real new
scope, not a UI-only wrapper around data that already fully exists.

**Financial Overview (12.6):** four numbers, all already-real underlying data (`Order.total` sum,
`WalletTransaction` commission sum, `WITHHOLDING_TAX_RATE`-derived withdrawal tax sum, the new
escrow balance above) — the smallest-scoped of the eight features, essentially a second dashboard
card row reusing Task 27's exact established pattern.

**Success criteria:**
- Admin sees the live retail/distributor ratio and gets an email alert when it drops below the
  admin-configured threshold (a real test: seed orders below 70% retail, confirm the alert fires;
  seed orders above, confirm it doesn't).
- Escrow balance increases by exactly 5% (or the admin-configured percentage) of each confirmed
  order's product revenue, verified against the database directly, not just the UI.
- At least Product/Category CRUD and Platform Settings changes are captured in a real,
  admin-visible audit trail with actor, timestamp, and what changed.
- Financial Overview's four numbers are correct against a seeded-data cross-check.

### 7. PDF/CSV Export Tooling (cross-cutting: 12.3's tax export + 12.4's report export)

**Builds on:** `WeasyPrint` is already a project dependency (Task 18e, invoice PDF generation —
works in CI's real environment; still locally blocked on this Mac per the already-documented
`project_weasyprint_pango_blocked_locally` limitation, so this feature's local verification will
hit the same wall Task 18e did and should plan for CI-verified `pytest.mark.skipif` coverage the
same way). CSV export needs no new dependency — Python's own `csv` stdlib module, matching
`SPEC.md`'s "no new dependency without a Boundaries check" default (a stdlib module isn't a new
dependency in the sense that check exists to gate).

**Scope:** not a separate feature so much as a shared capability every report in Feature 5 and the
existing tax-export line item in Section 12.3 (already-shipped, not yet exportable — confirmed via
grep of `apps/admin_portal`, no CSV/PDF export exists on the withdrawal/tax screens today) needs.
Should almost certainly be built as one shared `export_as_csv(queryset_or_rows)` /
`export_as_pdf(template, context)` utility, reused everywhere, rather than one-off per report —
matching this codebase's own repeated "extract shared, don't duplicate" convention (Task 19a's
`reverse_ancestor_pv` extraction, Task 33's `walk_sponsor_chain_downline_ids` extraction).

**Success criteria:**
- Every report from Feature 5, plus the existing GRA withholding-tax export (12.3), is downloadable
  as both CSV and PDF.
- Exported CSV/PDF figures match the live report screen exactly (a real cross-check test, not just
  "the file downloads without erroring").

### 8. Notification Template Editor + Provider-Choice Settings (source doc Section 13.8)

> SMS/Email Notifications global toggles; SMS Provider choice (Arkesel or Hubtel); Email Provider
> choice (Mailgun or Gmail SMTP); Sender Name; Sender Email Address; **Notification Templates —
> editable text for each notification type, admin changes wording without touching code.**

**Per direct user confirmation, this round builds an admin-editable *choice* field, not a second
real integration:** mNotify (SMS) and Gmail SMTP (email) stay the only wired providers in code,
exactly as they are today — this closes the gap between 13.8's already-visible-but-decorative
settings and reality without adding new Boundaries-gated external dependencies. Real Arkesel/Hubtel/
Mailgun integration is explicitly out of scope for this pass, flagged as future work if ever
requested (matching this project's existing "Currency" precedent from Task 36g/h: a working-looking
picker with nothing wired behind it is a real footgun, so either it's honestly locked to the one
real option or the label is written to say so — a decision the Plan phase should make explicitly,
not silently leave as a misleading always-enabled dropdown).

**Notification Template Editor — the one genuinely new capability here:** every outbound
SMS/email/in-app notification this codebase already sends (OTP codes, KYC decision, withdrawal
approved/rejected/paid, binary bonus credited, downline joined, direct referral bonus paid, PV
expiry warning — Task 21d's own 6 event types plus every auth/transactional message since Task 4)
is currently a hardcoded string somewhere in Python. This feature needs a real template-storage
model (name/subject/body with placeholder variables like `{{distributor_name}}`/`{{amount}}`) and
every existing send-site switched to render from it — a genuinely broad refactor touching most of
`apps/notifications`, `apps/accounts`, `apps/distributors`, `apps/withdrawal`, `apps/commissions`,
not a small addition. Sender Name / Sender Email Address are much smaller — already-real settings
values (`DEFAULT_FROM_EMAIL`, an mNotify sender-id config) that just need to move from `.env`/
hardcoded into `apps.platform_settings.config` as real constance fields.

**Success criteria:**
- SMS Provider / Email Provider fields exist, save, and are visibly labeled as the one real
  wired option (no functional no-op picker, matching the Currency-lock precedent).
- Admin can edit the wording of at least the highest-traffic notification types (OTP, withdrawal
  status, KYC decision) from `admin_portal`, and a real send uses the edited wording, verified
  live — not just that the edit saves.
- Sender Name / Sender Email Address are real, admin-editable, and actually used on the next real
  send (not read from `.env` anymore for these two specific values).

### 9. Wishlist (source doc Section 4.2, found gap — included per user confirmation)

> "Wishlist — save products to come back to later."

**Builds on:** `apps.orders.cart.Cart`'s session-based pattern is the wrong model to copy here — a
wishlist should persist for a logged-in user across devices/sessions (the cart's whole design
point, by contrast, is that it doesn't need an account). A small `Wishlist`/`WishlistItem` model
(or a `ManyToManyField` from `User` to `Product` if no extra per-item metadata is ever needed) tied
to `request.user`, guest-inaccessible (matching this codebase's own "no ownership check possible
without an account" pattern already established for `Cart` vs. `Order`).

**Success criteria:**
- A logged-in customer/distributor can add/remove a product to/from their wishlist from the product
  page, and see the full list from their account dashboard.
- The wishlist persists across a logout/login cycle (proves it's account-backed, not session-based).
- A guest sees a clear prompt to log in/register, not a broken control.

### 10. Saved / Multiple Delivery Addresses (source doc Section 4.2, found gap — included per user
confirmation)

> "Saved delivery addresses — they can save multiple addresses for faster checkout."

**Builds on:** `Order` already stores `address`/`area`/`landmark`/`delivery_zone` per order
(Task 17a), snapshotted at checkout — that snapshot behavior is correct and must not change (an
already-placed order's delivery address must never retroactively change if a saved address is later
edited). This feature adds a separate, reusable `Address` model owned by the customer, with
checkout gaining a "choose a saved address or enter a new one" step that still snapshots the chosen
address onto the `Order` exactly as today.

**Success criteria:**
- A logged-in customer can save, edit, and delete multiple delivery addresses from their account.
- At checkout, a saved address can be selected and correctly pre-fills/snapshots onto the new
  `Order`, without changing how a guest (no account) checks out today.
- Editing a saved address after an order was placed with it does not change that already-placed
  order's stored address (a real regression test, matching this codebase's existing snapshot
  convention elsewhere).

## New / Extended Admin Settings (Sections 13.5–13.8, 13.10–13.12)

`CLAUDE.md` already documents these as "stubbed with sane hardcoded defaults" in the MVP; confirmed
directly against `apps/platform_settings/config.py` which of each group's fields are real today:

| Group | Already real (constance) | Missing / new this phase |
|---|---|---|
| 13.5 Delivery & Shipping | `DELIVERY_FEE_KUMASI/ACCRA/OTHER_REGIONS`, `FREE_DELIVERY_THRESHOLD` | Delivery On/Off toggle, Default Delivery Fee, Estimated Delivery Days, Delivery Message |
| 13.6 Order Management | `PENDING_ORDER_AUTO_CANCEL_HOURS` | Auto-Confirm Orders toggle, Order ID Prefix, Order Invoice Footer Text, Backorders On/Off + Backorder Message (Feature 4) |
| 13.7 Tax | `WITHHOLDING_TAX_RATE` (lives in 13.4 today) | VAT Rate, Tax Display at Checkout, Tax-Exempt Products |
| 13.8 Notification & Communication | SMS/Email global toggles (implicit via `MNOTIFY_API_KEY`/email backend presence, not a real toggle) | Everything in Feature 8 above — provider choice, sender identity, template editor |
| 13.10 Product & Inventory | none | Low Stock Alert Threshold (currently a hardcoded `LOW_STOCK_THRESHOLD` Python constant, Task 27 — should become real), Out of Stock Behaviour (Feature 4), Product Review Approval (Feature 1), Maximum Images Per Product (currently hardcoded `MAX_PRODUCT_IMAGES = 5`, Task 26 — source doc says 8; a real Plan-phase decision, not silently changed here) |
| 13.11 Discount & Promotions | none | Discount Codes toggle, Maximum Discount Per Order, Discount Applicable To (Feature 2) |
| 13.12 Compliance | none | Retail PV Minimum (%), Compliance Alert Email, Escrow Reserve Percentage (%), Audit Log Retention Period (Feature 6) |

Every new setting follows this codebase's existing rule: read live from `constance.config` at the
point of use, never hardcoded, and added to `admin_portal`'s Platform Settings screen (Task 28) in
its correct fieldset tab — never a new raw-Django-Admin surface, matching Task 22/23/26/27/28/29's
established "every admin-facing screen looks like the rest of the app" precedent.

## Data Model Additions (indicative, not final — Plan phase owns the real schema)

New apps or models likely needed: `apps/reviews` (or extend `apps/catalog`), `apps/promotions`
(`DiscountCode`, `Banner`), `apps/reporting` (report query layer + any pre-aggregation tables the
Plan phase decides on), `Escrow`/`ComplianceSnapshot` model (likely `apps/platform_settings` or a
new `apps/compliance`), `NotificationTemplate` (`apps/notifications`), `Wishlist`/`WishlistItem`
(`apps/catalog` or a new `apps/wishlist`), `Address` (`apps/accounts` or `apps/orders`). None of
these are schema decisions this document makes final — `SPEC.md`'s Boundaries already require
asking first for "any database schema/migration change after the initial MVP schema is reviewed,"
and that applies to every model above.

## Testing Strategy (delta from `SPEC.md`)

`SPEC.md`'s existing elevated-rigor rule (`apps/commissions`, `apps/wallet`, `apps/withdrawal`
services need tests for every code path before merge) extends to two new areas this phase touches
real money: **Discount Codes** (Feature 2 — concurrent redemption, expiry/audience edge cases,
correct total recomputation) and the **Escrow ledger** (Feature 6 — the running-balance credit at
order confirmation). Both need the same weak-leg-cap-rounding level of test coverage this codebase
already holds `apps/commissions` to, not standard coverage. Reporting/export correctness (Features
5/7) needs seeded-data cross-checks (exported figures == database reality), not just "the page/file
renders."

## Boundaries (Phase-2-specific, on top of `SPEC.md`'s existing three-tier list)

- **Always:** every new setting lives in `apps.platform_settings.config` and is surfaced in
  `admin_portal`'s Platform Settings screen, not raw Django Admin; every money-adjacent change
  (Discount Codes, Escrow) gets a `doubt-driven-development` pass before implementation, matching
  every prior money-adjacent feature in this codebase.
- **Ask first (new this phase):** the exact schema for each new model listed above (per `SPEC.md`'s
  existing post-MVP migration-review rule); whether `MAX_PRODUCT_IMAGES` changes from 5 to the
  source doc's 8 (a live-shipped MVP value, not a fresh decision); the pre-aggregation-vs-live-query
  design for Feature 5's reporting at scale.
- **Never (new this phase):** attempt a real GCB Bank API integration for the escrow tracker
  (internal ledger only, per direct user confirmation above); wire a second real SMS/email provider
  without a separate explicit go-ahead (per direct user confirmation above); let the Notification
  Template Editor's admin-entered placeholder text render unescaped into an SMS/email/HTML context
  (the same "constrain server-side, never raw interpolation" rule Task 17/37 already established
  for this exact class of admin-entered-text-into-a-rendering-context risk).

## Success Criteria (overall)

Phase 2 is done when: all ten features above meet their own success criteria; every new admin
setting is live-editable from `admin_portal`, not hardcoded; full test suite green including new
elevated-rigor coverage for Discount Codes and Escrow; CI green on real MySQL; each feature
live-browser-verified against a real `runserver` session, not just pytest (matching this project's
own standing verification convention); `SPEC.md` updated to point at this document instead of
saying "deferred to a later spec."

## Open Questions

Resolved via direct user confirmation this session (recorded here for the Plan phase to inherit,
not re-litigate):
1. **Scope:** all ten features are in this spec now; build order/priority is a Plan-phase decision.
2. **Escrow tracker:** internal ledger only, no real GCB Bank integration.
3. **SMS/Email provider switching:** admin-editable choice field only; mNotify/Gmail stay the only
   wired providers.
4. **Wishlist + saved addresses:** included in scope (Features 9/10 above).
5. **Review scope (resolved 2026-08-12, before Task 41a started):** one review per customer per
   product, not one per order — edit-in-place on a repeat purchase, enforced via `Review`'s
   `UniqueConstraint(user, product)`.

Still open, flagged for the Plan phase rather than decided here:
- Discount code: is the usage limit a global cap only, or also a 1-per-customer default?
- `MAX_PRODUCT_IMAGES`: stay at 5 (current shipped MVP value) or move to the source doc's 8?
- Reporting at scale: pre-aggregated rollup tables vs. a scheduled report-cache job vs. a bounded
  live-query lookback window — a real architectural decision, not a detail.
- Which existing models beyond `Order`/`Distributor`/`WithdrawalRequest` should gain
  `HistoricalRecords()` for the audit log (Feature 6), and whether the audit log needs its own
  dedicated event-stream model instead of (or alongside) `simple_history`.
- Build order across Tasks 42-48 (Phases 14-16) — not decided in this document; see `tasks/plan.md`
  for the risk-grouping rationale used so far.
- **The retail/distributor compliance ratio (Task 47b, flagged by CodeRabbit on PR #73):** this
  document names `Order.pv_earned > 0` as the classifier but doesn't yet define the actual formula
  — which `Order.status` values count in the numerator/denominator, how a refund/cancellation
  after the fact is treated, and the monthly window's timezone/boundary. Needs a direct answer
  before 47b's implementation, not assumed.
- **Escrow reversal + idempotency (Task 47a, flagged by CodeRabbit on PR #73):** this document
  specifies the credit-on-confirmation side but not what happens on a later cancellation/refund
  (Task 18b already reverses PV for that case — does escrow reverse too?) or how a retried/replayed
  webhook is prevented from double-crediting the same order. Needs resolution as part of 47a's own
  `doubt-driven-development` pass, not decided here.
- **Notification template rendering safety (Task 48a, flagged by CodeRabbit on PR #73):** the
  Boundaries section below states one generic "constrain server-side, never raw interpolation"
  rule, but SMS, email, and HTML are three different unsafe-character profiles (CRLF header
  injection for email, length/control-character limits for SMS, HTML escaping for the in-app
  channel) — 48a needs context-specific validation per channel, not one rule applied uniformly,
  with a test per rendering context.

## Agent Skills for the Plan/Implement Phases

Per `CLAUDE.md`'s Agent Skills Workflow and this project's standing "check the full 24-skill
catalog per task, not a habitual few" convention, checked explicitly against Phase 2's actual shape
rather than assumed:

- **Next immediate step:** `agent-skills:planning-and-task-breakdown` — turn this spec into
  `tasks/plan.md`/`tasks/todo.md` entries (a new Phase 11+), vertically sliced per feature the same
  way every MVP task was.
- **Per feature, before implementation:** `agent-skills:doubt-driven-development` for Features 2
  (Discount Codes) and 6 (Escrow) specifically — both are money-adjacent and interact with
  already-shipped locked/idempotent code paths (checkout, order confirmation).
- **During every feature's build:** `agent-skills:incremental-implementation` +
  `agent-skills:test-driven-development` (this codebase's standing default for all non-trivial
  logic), `agent-skills:frontend-ui-engineering` for every customer/admin-facing screen (all ten
  features need one), `agent-skills:security-and-hardening` specifically for Feature 8's template
  editor (admin-entered text rendered into SMS/email/HTML — a real injection surface) and Feature 2
  (checkout-adjacent user input).
- **Cross-cutting:** `agent-skills:api-and-interface-design` for the shared `export_as_csv`/
  `export_as_pdf` utility (Feature 7) so every report reuses one real interface instead of drifting
  per-report; `agent-skills:performance-optimization` for Feature 5's reporting queries specifically,
  given `SPEC.md`'s explicit hundreds-of-thousands-of-users scale target; `agent-skills:
  observability-and-instrumentation` for Feature 6's audit log (the feature *is* observability);
  `agent-skills:documentation-and-adrs` for the real architectural decisions flagged as Open
  Questions above (reporting-at-scale design, audit-log model choice) — each gets its own ADR the
  same way Tasks 4/9/12/13/16/17/18/19/20/35 already did, not folded silently into a PR description.
- **After every slice, no matter how small:** `agent-skills:code-review-and-quality` +
  `agent-skills:code-simplification`, matching this codebase's standing post-build convention;
  `agent-skills:debugging-and-error-recovery` if anything breaks; full test suite (not just new
  tests) before moving to the next slice.
- **Checked and deliberately not front-loaded here** (apply when the moment actually calls for
  them, not before): `agent-skills:ci-cd-and-automation`, `agent-skills:git-workflow-and-versioning`,
  `agent-skills:shipping-and-launch`, `agent-skills:deprecation-and-migration`,
  `agent-skills:browser-testing-with-devtools`, `agent-skills:context-engineering`,
  `agent-skills:using-agent-skills` — all execution-phase or situational, nothing to apply yet at
  the Specify stage this document represents. `agent-skills:idea-refine` and `agent-skills:
  interview-me` were considered for this spec itself but not separately invoked: `agent-skills:
  spec-driven-development`'s own Phase 1 ("surface assumptions," reframe vague asks as success
  criteria) already covers the same ground the four `AskUserQuestion` questions above resolved, so
  running a second divergent-thinking pass on top would have been redundant, not additive.
