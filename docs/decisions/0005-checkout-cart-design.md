# ADR-0005: Checkout & cart design — delivery zones, cart persistence, order lifecycle start, PV branching

## Status

Accepted (2026-07-24)

## Date

2026-07-24

## Context

Task 17 (Cart + checkout) has no prior design to draw on — Tasks 1-16 built catalog, auth, the
binary tree/PV ledger, the commission engine, and the wallet/withdrawal path, but nothing in this
codebase has ever created an `Order`. `SPEC.md` gives the business shape (cart review, delivery vs.
pickup, zone-based delivery fee, Paystack payment, PV only for distributor purchases) but Open
Question #4 (exact delivery zone fee table and free-delivery threshold) was never given real
numbers — the docs just say "set by admin." `apps/catalog/services.py::decrement_stock` already
exists and its own docstring explicitly defers to "the real checkout/order pipeline (Phase 6)
[which] will call this from within its own transaction once it exists" — that pipeline is this
task. `templates/catalog/product_detail.html`'s add-to-cart button is an "honest disabled stub"
(Task 8) waiting on this task to become real.

Per `SPEC.md`'s Boundaries, database schema changes and Paystack integration code both require
explicit sign-off — this ADR resolves the design up front (mirroring ADR-0004's process for Task
16), with implementation-time sign-off still required before the schema migration and the
Paystack-call slices specifically, tracked per-slice in `tasks/todo.md`.

The primary source doc (`docs/Bancostore_Features_and_Workflow_v4.docx`, Section 5.1-5.3) was
re-read directly rather than relied on from memory, per this project's established
`source-driven-development` discipline (the same doc's own worked examples already corrected this
project's Direct Referral Bonus and Matching Bonus formulas earlier) — its exact wording informs
several decisions below.

## Decision

### 1. Delivery zone fees — the source doc's own worked example, not a guess (user-confirmed)

Section 5.1 gives its own concrete example: *"Within Kumasi = GHS 20, Accra = GHS 50, Other
regions = GHS 70."* An initial proposal using different numbers (Accra cheapest, reasoning the
business is Accra-based) was raised, then corrected against this primary source once found —
matching how every other seeded default in this project traces to either the source doc or an
explicit user decision, never an assumption. Seeded as three new `django-constance` settings
(`DELIVERY_FEE_KUMASI`, `DELIVERY_FEE_ACCRA`, `DELIVERY_FEE_OTHER_REGIONS`, each using the existing
`non_negative_money_field` widget, matching `WITHHOLDING_TAX_RATE`'s neighbors) plus
`FREE_DELIVERY_THRESHOLD` (GHS 500, same widget). All four remain fully admin-editable at runtime —
this is only the starting seed value, exactly like Open Question #5's resolution for Task 16.
Pickup is always free (no fee lookup, no threshold logic — a hardcoded zero, since "free" is
pickup's whole definition per Section 5.1's own wording, not a business rule that varies).

### 2. Cart is session-based for every user, guest or logged-in — no `Cart`/`CartItem` DB model

`SPEC.md` line 11 states customers "checkout as guest or with an account" — the account
requirement (where one applies) is about checkout identity/contact info, not about cart storage.
No requirement anywhere states a logged-in customer's cart must survive a device switch or a
session expiry. Given that, the cart itself lives entirely in `request.session` (already
Redis-backed per this project's Task 1-3 scaffold — durable enough for a shopping session, not
durable across devices) via a small `apps/orders/cart.py::Cart` wrapper class (`{product_id:
quantity}` — corrected from an earlier `variant_id` draft of this decision; no interactive variant
selection exists anywhere in this codebase yet, see the Task 17a/17b schema review notes below),
identical in shape whether the visitor is anonymous or logged in. This avoids a
`Cart`/`CartItem` DB model, a merge-on-login reconciliation path, and an abandoned-cart cleanup job
— three real pieces of speculative complexity with no stated requirement driving them.

**Rejected: DB-backed `Cart`/`CartItem` model with guest-session-to-account merge-on-login.**
Standard ecommerce pattern, and defensible, but every one of its extra moving parts (merge
conflict resolution when the same product exists in both a guest cart and a saved account cart,
an abandoned-cart cleanup task, migration surface for a model with no PV/commission/wallet
implications) is unjustified against an MVP requirement that only asks for guest-or-account
checkout, not cross-device cart persistence. Revisit if user demand for "my cart follows me across
devices" surfaces post-launch — a real Phase 2 candidate, not silently foreclosed.

### 3. `Order` is created at checkout confirmation, in a `pending` state — before payment, not after

This is a deliberate divergence from Task 16's registration-payment precedent
(`PendingRegistration` holds data in a *separate, non-authoritative* table until payment succeeds,
and the real `Distributor` row is only created then). Section 5.2's own order-status table starts
at **"Pending — Order placed but payment not yet confirmed"** as a real, admin-and-customer-visible
state, and Task 18's acceptance criteria requires unpaid orders to auto-cancel after a configured
window — both only make sense if a real `Order` row exists from the moment checkout is confirmed,
not only after payment succeeds. So `Order`/`OrderItem` are created transactionally the moment the
customer confirms their order summary (cart converted to line items, delivery fee and total
snapshotted), status `pending`, with a pinned `payment_reference` generated the same way
`Distributor.starter_pack_payment_reference` is — then `initialize_transaction` is called and the
customer is redirected to Paystack. Stock is **not** decremented at this point (see decision 5).

### 4. Payment confirmation reuses the established idempotent-consume pattern, targeting `Order` directly

Mirrors `consume_paid_starter_pack`'s shape exactly: a webhook/callback triggers
`confirm_order_payment(reference)`, which locks the `Order` row by `payment_reference`
(`select_for_update_nowait_if_supported`), no-ops if already `confirmed` (idempotent against a
webhook/callback race, identical reasoning to every other Paystack confirmation path in this
project), calls `verify_transaction(reference)` and checks the exact pinned amount, then — inside
that same lock — decrements stock per line item, credits PV via `record_purchase_pv` **only if the
purchasing user is in the `distributor` group** (decision 6), transitions `Order.status` to
`confirmed`, and sends the SMS+email confirmation *after* the lock releases (matching Task 16g's
"never hold a lock across external I/O" standard, not Task 12's older in-lock precedent).

### 5. Stock is checked and decremented at payment confirmation, not reserved at add-to-cart

No stock hold/reservation exists while an item sits in a cart (session or otherwise) — the same
"truth lives at the point of commit, not at the point of intent" philosophy `decrement_stock`
already embodies with its own `select_for_update`-based concurrency safety. Two customers can both
have the last unit of a product in their cart simultaneously; only whichever one's payment
confirms first will successfully decrement stock. If a later confirmation finds insufficient stock,
`confirm_order_payment` must fail cleanly (the order is left in a distinct failure state, the
customer is refunded — see Consequences) rather than silently over-selling. **Rejected: reserve
stock for a time-limited window when an item is added to cart.** Real ecommerce pattern, but adds a
scheduled-expiry mechanism and a "why did my cart item disappear" support question for no stated
MVP requirement — revisit only if checkout-time stock-conflict rate turns out to be a real problem
in practice, not preemptively.

### 6. PV is credited only when the purchasing account is in the `distributor` group — never for a `customer`

`apps/accounts/permissions.py::is_distributor` (already used throughout `apps/distributors`) is the
single check `confirm_order_payment` uses to decide whether to call `record_purchase_pv`. A guest
checkout (no account at all) can never be a distributor purchase — PV is skipped unconditionally
for any order with no authenticated `distributor`-group user attached. This is the literal
acceptance criterion from `tasks/todo.md` ("a distributor's purchase generates PV... a regular
customer's does not"), made explicit here because it is also a real fraud-adjacent boundary: PV
must never be inferable from order contents or amount alone, only from the account's actual role at
confirmation time.

## Alternatives Considered

### Zone fees keyed by an admin-editable region table (many rows) instead of three fixed constance settings

- Pros: scales to more than three zones without a code change.
- Rejected: the source doc's own example is a flat three-tier structure (Kumasi / Accra / other),
  and `SPEC.md`'s constance pattern already handles "admin can change the number" for the *existing*
  three zones without needing a full CRUD-managed zone table. A future move to a real zone model is
  a bigger, separately-justified change (matching this project's repeated preference for the
  simplest thing that satisfies the stated requirement) — not built preemptively here.

### DB-backed cart with guest-to-account merge on login

Covered under decision 2 above.

### Decrementing stock at add-to-cart time (reservation)

Covered under decision 5 above.

## Consequences

- **A new `apps/orders` app is required** (`Order`, `OrderItem` models; `apps/orders/cart.py`'s
  session wrapper; `apps/orders/services.py` for `DeliveryFeeCalculator` and
  `confirm_order_payment`) — new database schema, so implementation still needs the explicit
  ask-first sign-off `SPEC.md`'s Boundaries require, same as Task 16's schema slices.
- **A new Paystack integration surface is required at the Transaction (charge) side** — reuses
  `initialize_transaction`/`verify_transaction` (already built and proven for registration/starter-
  pack payments), not a new Transfer-side surface like Task 16e/16f. Still needs its own sign-off
  and `security-and-hardening` pass per the Boundary — a new call site with new metadata (line
  items, delivery address) is new attack surface even reusing an existing wrapper.
- **A failed/insufficient-stock confirmation needs its own terminal-ish handling**: since stock
  isn't reserved at cart time, a payment that verifies successfully but hits an out-of-stock line
  item at confirmation time must both refund (or hold for manual admin resolution — TBD at 17's
  implementation time, flagged here rather than silently assumed) and clearly communicate this to
  the customer, distinct from an ordinary payment failure.
- **The order status lifecycle spans two tasks**: Task 17 only needs `pending` -> `confirmed` — no
  invented "payment-failed" state (corrected in the Task 17a Schema Review below); a payment that
  never completes simply stays `pending` until Task 18's auto-cancel-unpaid job converts it to
  `cancelled`, matching Section 5.2's own state table exactly. The full stage list (`Processing`,
  `Dispatched`, `Delivered`, `Cancelled`, `Refunded`, and their admin-facing management view) is
  Task 18's scope per Section 5.2/5.3 — `Order.status` is modeled with the full 7-choice set from
  the start (cheap, avoids a later migration) even though Task 17 only transitions through the
  first two.
- **Guest checkout means `Order.customer` (or equivalent FK) must be nullable**, with contact
  details (name, phone/email, delivery address) captured directly on the order for guests rather
  than looked up from an account — mirrors `PendingRegistration`'s own flat address shape
  (`address`, `area`, `landmark` — Ghana's landmark-based addressing convention, already
  established in this codebase) rather than inventing a new address shape.

## Task 17a Schema Review (2026-07-24) — 8 findings from a fresh adversarial review, all resolved before the migration

A `doubt-driven-development` cycle (single-model, fresh-context, adversarial — cross-model offered
and declined by the user as unnecessary given the findings were already concrete and grounded in
real code) ran against the proposed `Order`/`OrderItem` schema before any migration existed. It
found 8 real issues; all folded in below rather than deferred.

**PV must be snapshotted, not re-derived live at confirmation — the single most important finding.**
The original schema draft had no PV field anywhere. Since `Order` is created `pending` *before*
payment (decision 3 above) and `apps.pv_ledger.services.record_purchase_pv(distributor, pv_amount)`
takes a pre-computed integer, whoever calls it in 17d would have had to read `Product.pv_value`
*live* at confirmation time — the exact class of bug this ADR's decision 5 (money/fee snapshots)
already exists to prevent, just not originally extended to PV. `Product.pv_value` is admin-editable
in Django Admin with nothing tying it to in-flight orders. Fixed: `OrderItem.unit_pv` (a snapshot,
set at order-creation time, same as `unit_price`) plus `Order.pv_earned` (the aggregate actually
credited, set inside the same locked block as `record_purchase_pv` at confirmation, 0 until then) —
this second field also satisfies Task 17a's own acceptance-criteria line calling for a
"PV-eligibility flag," made more useful as an actual credited amount than a bare boolean.

**`Order.email` must be optional, not required — this codebase already has a documented precedent
for exactly this gap.** `apps/distributors/forms.py`'s email field is `required=False`
(`SPEC.md` Section 4: "for notifications only"), and two call sites in `apps/distributors/views.py`
already synthesize a fake `@bancostore.test` address purely to satisfy Paystack's API, explicitly
commented as "without claiming it's a real contact channel." A required `Order.email` would have
either broken checkout for an email-less distributor or silently reinvented that same workaround —
worse, ADR decision 4's order-confirmation email would then target a fake, undeliverable address
with no error ever surfacing. Fixed: `email = models.EmailField(blank=True, default="")`; 17d's
order-confirmation email send must skip a blank email rather than invent one.

**No variant tracking on `OrderItem` — a genuine inconsistency between this ADR's own decision 2
("cart keyed `{variant_id: quantity}`") and the actual state of the codebase, resolved by correcting
the wording, not by adding a field.** `apps/catalog/models.py::ProductVariant` display in
`templates/catalog/product_detail.html` (lines 60-73) is currently plain, non-interactive `<span>`
elements — there is no selection mechanism anywhere for a customer to actually choose a variant, and
Task 7's own stated scope ("stock is tracked at the Product level only... full variant stock
tracking, if ever needed, is a separate future task") never built one. `OrderItem.product` is a bare
FK to `catalog.Product` (matching `apps.catalog.services.decrement_stock(product, quantity)`'s own
signature); the cart (17b) is keyed by product id, not variant id — this ADR's decision 2 wording
above should be read as corrected to say "product," not "variant." Revisit together with real
interactive variant selection if that's ever built — not silently dropped, genuinely out of scope.

**`Order.customer` uses `on_delete=SET_NULL`, which cannot distinguish "always a guest" from
"account since deleted" — accepted as a trade-off, not fixed with a new field.** No
account-deletion feature exists anywhere in this codebase to trigger this ambiguity today, and
`Order`'s own contact-info snapshot (name/phone/email) stays intact either way, so no
support/audit-relevant information is actually lost. Revisit if account deletion is ever built.

**Two mechanical fixes**: `CheckConstraint` OR-branches now use explicit parens around each
alternative, matching `WithdrawalRequest`/`Distributor`'s existing both-or-neither constraints
exactly (readability convention, not a behavior change — Python's operator precedence already
evaluated the original correctly). `OrderAdmin` carries a forward-pointing docstring comment citing
this ADR's own §9-equivalent finding from ADR-0004 (Django admin actions default to requiring
`has_change_permission`, so an unconditional `False` silently blocks bulk actions for everyone,
superuser included, unless each action declares `permissions=["view"]`) — so Task 18, which adds
the real bulk status-update/cancel/refund actions, doesn't rediscover a bug this project already
found and fixed once.

**Flagged, not fixed at the schema level (not schema-fixable): `Order.subtotal` has no DB-level
guarantee of equaling `sum(OrderItem.unit_price * quantity)`.** A `CheckConstraint` cannot reference
another table in Django/MySQL. This is a required test-coverage item for 17c/17d's service layer
(the actual guardrail for this identity), not a gap in this schema — flagged explicitly rather than
silently assumed sufficient, mirroring how `WithdrawalRequest`'s own arithmetic constraint
(ADR-0004) was still found missing a term by CodeRabbit despite being reasoned through carefully.

## Update (2026-07-25): Decision 5's stock-insufficiency handling, resolved at 17d implementation time

Decision 5 above left "the exact terminal handling (refund vs. manual admin resolution)... TBD at
implementation time, flagged here rather than silently assumed" — resolved directly with the user
during Task 17d: when `confirm_order_payment` finds insufficient stock after Paystack has already
verified a successful payment, the `Order` transitions to `cancelled` (not left `pending`, which
would otherwise retry the same failing decrement on every one of Paystack's webhook redeliveries
for up to 72 hours), logged at `ERROR` level for a human to action the actual GHS refund.

**Rejected: automated Paystack Refund API integration as part of 17d.** This project has never
integrated Paystack's Refund API anywhere — doing so here would be new payment-integration surface
requiring its own `security-and-hardening` pass and sign-off (per `SPEC.md`'s Boundary on Paystack
integration code), a larger scope increase than 17d's own remit. The race this applies to is
narrow by construction (decision 5 above: no stock reservation at cart time), so full automation on
first implementation isn't proportionate. Task 18 — which already owns the rest of the
`cancelled`/`refunded` lifecycle per this ADR's own Consequences section — is the natural home for
real refund automation, tracked there rather than assumed done here.
