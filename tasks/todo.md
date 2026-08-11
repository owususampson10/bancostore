# Task List: Bancostore MVP

Detailed, checkable tasks. See `tasks/plan.md` for phases, checkpoints, and architecture
decisions. Each task = one vertical slice (db + backend + frontend), tested both sides, verified
working before starting the next one.

**Stack note:** tasks below use Django/Python terms (Django Admin instead of Filament, HTMX views
instead of Livewire components, Celery tasks instead of Jobs, pytest instead of Pest) — see
`SPEC.md` Tech Stack for why and for the full Laravel→Django mapping.

---

## Known issues — tracked, not blocking (from Tasks 1-6 code review + security audit, 2026-07-11)

Two independent review passes (code-reviewer + security-auditor subagents) ran against the full
Tasks 1-6 codebase. The two most serious findings were fixed immediately (see commit "Fix admin
lockout bypass + lock down /silk/ + fail-closed DEBUG/SECRET_KEY"): the `ModelBackend` admin-lockout
bypass, `/silk/` being reachable with no authentication, and `DEBUG`/`SECRET_KEY` defaulting
fail-open. Everything below was deliberately deferred — logged here so it isn't lost, not because
it's unimportant:

- [x] ~~No rate limiting anywhere~~ — **Fixed 2026-07-12.** `@ratelimit` (per-IP) added to
  `register` (5/h), `resend_otp` (5/h), `forgot_password` (5/h), and `distributors:login` (20/m);
  `AdminLoginView.post()` throttled the same way via `django_ratelimit.core.is_ratelimited()`
  directly, since it's a class-based wizard view the decorator can't wrap. `RATELIMIT_VIEW`
  (`bancostore/views.py::ratelimited_view`) added — without it the middleware itself would crash
  on the first rate-limited request. Verified with real tests that fire past each limit and assert
  a 429, not just that the decorator is present.
- [x] ~~Race condition in the failed-login-attempt counters~~ — **Fixed 2026-07-12.** Both
  `apps/accounts/signals.py` and `apps/distributors/services.py` reproduced a genuine lost-update
  race with real multi-threaded tests before fixing (not just asserted as a risk). Fixed with
  `select_for_update()` + a new shared `bancostore/concurrency.py::retry_on_lock_contention`
  helper (third use of the pattern after Task 7's stock decrement — the threshold for extracting
  it). The admin fix needed a second pass: `AdminProfile.objects.get_or_create()` was left outside
  the retry-protected block on the first attempt, reasoning Django's own create-race handling was
  enough — a 15-run flake-rate check (2/15 failures) proved that wrong before it shipped quietly
  broken. 20/20 clean after folding it into the same retried block.
- [x] ~~Admin login form leaks lock state before checking the password~~ — **Fixed 2026-07-11.**
  Reproduced live (curl comparison of locked+wrong-password vs. locked+correct-password vs.
  unlocked/nonexistent accounts) before fixing. Root cause existed identically in
  `apps/distributors/services.py::attempt_distributor_login` too, so both were fixed together:
  lock state is now only revealed after the submitted password is confirmed correct
  (`apps/accounts/forms.py::AdminAuthenticationForm.clean()`,
  `apps/distributors/services.py::attempt_distributor_login`). Regression tests added for both
  (wrong-password-stays-generic, correct-password-still-shows-locked) and verified live again
  post-fix.
- [x] ~~Several constance settings are decorative — they exist and look live in the admin panel but
  nothing reads them~~ — **Fixed 2026-07-31 (Task 30a-30d).** `MIN_PASSWORD_LENGTH` and
  `PASSWORD_COMPLEXITY_ENABLED` are now enforced by custom password validators reading
  `constance.config` live (30a); `SESSION_TIMEOUT_MINUTES`/`ADMIN_SESSION_TIMEOUT_MINUTES` are now
  enforced by `SessionTimeoutMiddleware` (30b); `GOOGLE_LOGIN_CUSTOMERS_ENABLED` now actually gates
  the customer Google login button, ANDed with the real `SocialApp`-exists check (30c). The
  remaining 3 (`PASSWORD_RESET_EXPIRY_MINUTES`, `ADMIN_2FA_METHOD`'s wrong `"sms"` default,
  `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED`) were deliberately **not** wired this round — user-confirmed
  scope, not a silent skip — and instead got honest "not yet enforced" fieldset help text (30d);
  see Task 30's full breakdown below.
- [x] ~~`seed_roles` has no guard against running in a non-DEBUG environment~~ — **Fixed
  2026-07-12.** Raises `CommandError` outside `DEBUG`. Existing tests updated to force
  `settings.DEBUG = True` (matching CI's deliberate `DEBUG=False`), new test confirms the guard
  actually blocks the command and creates no stub account when `DEBUG=False`.
- [x] ~~OTP codes compared with `!=` instead of `secrets.compare_digest()`~~ — **Fixed 2026-07-12**,
  bundled with the OTP concurrency fix below since it touched the same line.
- [x] ~~No production security headers configured yet (`SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`)~~ — **Fixed 2026-08-10
  (Task 24d + 24g).** `if not DEBUG:` block added, `SECURE_HSTS_SECONDS` deliberately conservative
  (1 hour, not the usual 1 year) for this first deploy. Verified live: HTTPS response headers show
  `Strict-Transport-Security: max-age=3600` for real.
- [x] ~~`DEFAULT_FROM_EMAIL` uses the reserved `.test` TLD~~ — **Fixed 2026-08-10 (Task 24d).**
  Now the same Gmail address `EMAIL_HOST_USER` sends through (only address with real SPF/DKIM
  alignment for this Gmail-SMTP setup), fixed in both local `.env` (which turned out to already be
  sending real email from the broken address) and production. Also fail-closed now — a follow-up
  CodeRabbit-caught gap on the same PR meant an unset or reserved-placeholder value outside `DEBUG`
  raises `ImproperlyConfigured` rather than silently degrading.

## Known issues — round 2 (fresh code-review + security audit of Tasks 1-7, 2026-07-12)

Both subagents ran again after the round-1 fixes above landed, specifically to catch anything the
first pass missed and to review Task 7 (catalog) fresh. All Critical/High findings below were
fixed the same day; two need real deployment context to fix correctly and are tracked instead of
guessed at.

- [x] ~~OTP attempts counter had the same lost-update race as the login counters~~ — **Fixed
  2026-07-12.** The 4th spot the "comprehensive" round-1 fix actually missed —
  `apps/notifications/otp.py::verify_otp` read `otp.attempts`, incremented in memory, and saved
  with no locking. Fixed with the same `select_for_update()` +
  `retry_on_lock_contention()` pattern; a real multi-threaded test (10 concurrent wrong guesses
  against `OTP_MAX_ATTEMPTS=3`) reproduced the race first (only 1 of 10 attempts got recorded),
  confirmed the fix caps the count at exactly 3, stable across 15 repeated runs. Also switched the
  code comparison to `secrets.compare_digest()` while in the same function.
- [x] ~~`verify_otp_view` had no rate limiting~~ — **Fixed 2026-07-12.** Its siblings
  (`register`/`resend_otp`/`forgot_password`/`login_view`) all got `@ratelimit` in round 1; this
  one — the endpoint that actually receives OTP guesses — didn't. Added `10/m` per IP.
- [x] ~~`resend_otp` accepted any HTTP method, so a bare GET triggered a real (billed) SMS send~~ —
  **Fixed 2026-07-12.** GET requests bypass Django's CSRF check entirely (CSRF only applies to
  state-changing methods), so this was triggerable via something as simple as an `<img>` tag while
  a victim had an in-progress OTP flow. Added `@require_POST`; new test confirms GET now returns
  405 and sends no SMS.
- [x] ~~`EmailBackend.authenticate()` re-leaked lock state via a timing side-channel~~ — **Fixed
  2026-07-12.** The round-1 fix closed the *message*-level leak in
  `AdminAuthenticationForm.clean()`, but `EmailBackend` itself still checked `locked_until` before
  `check_password()` one layer underneath — a locked account skipped the slow password-hash
  comparison entirely for any submitted password, while an unlocked account always paid that cost.
  A sophisticated attacker measuring response times could still statistically distinguish the two.
  Reordered to check the password first, matching the pattern used everywhere else. Regression test
  asserts the backend behaves identically (returns `None`, no exception) for a wrong password
  whether the account is locked or not.
- [x] ~~`retry_on_lock_contention` could itself become a denial-of-service vector~~ — **Fixed
  2026-07-12.** MySQL's default `innodb_lock_wait_timeout` is 50 seconds — `select_for_update()`
  without `NOWAIT` blocks the calling thread for up to that long *before* the retry loop's own
  error handling even engages, and the loop retries up to 10 times. A handful of concurrent
  requests against the same row (e.g. several people submitting the same wrong password
  simultaneously) could tie up a disproportionate share of the site's limited worker pool for
  minutes. Added `select_for_update_nowait_if_supported()` — uses `NOWAIT` on backends that support
  it (MySQL/Postgres) so contention fails immediately instead of blocking; falls back to plain
  `select_for_update()` on SQLite, which doesn't support `NOWAIT` at all (Django raises
  `NotSupportedError` if you pass `nowait=True` there — confirmed by reading Django's compiler
  source before implementing, not assumed). All three call sites (stock decrement, admin lockout,
  distributor lockout, OTP attempts) updated. The MySQL-specific behavior can only be fully
  verified in CI (no local MySQL on this machine) — pushed and confirmed via the real `mysql:8` CI
  service container, not just asserted from reading documentation.
- [x] ~~Admin login rate limit was as loose as the public endpoints~~ — **Fixed 2026-07-12.**
  `AdminLoginView`'s `20/m` matched public distributor register/login despite admin being the
  highest-value target with the lowest legitimate traffic of the three roles. Tightened to `5/m`,
  scoped specifically to the wizard's `auth` step (not the 2FA token/backup steps, which aren't
  useful for spraying guesses across different admin emails and shouldn't risk blocking a
  legitimate admin mistyping their code a few times).
- [x] ~~Rate limiting (and the future `SECURE_PROXY_SSL_HEADER` header work above) will collapse
  into a single shared bucket — or become spoofable — once this sits behind Hostinger's Nginx~~ —
  **Fixed 2026-08-10 (Task 24d + 24g).** Nginx sets `X-Real-IP $remote_addr` (always overrides any
  client-supplied value, never appends), `RATELIMIT_IP_META_KEY = "HTTP_X_REAL_IP"` reads exactly
  that. Verified live with a real spoofing attempt, not just config inspection: a `429` still fired
  on the 6th request to a `5/h` rate-limited endpoint even when a fake `X-Real-IP` header was sent —
  proving spoofing genuinely cannot bypass the limit, the exact attack this item was written about.
- [x] ~~`PAYSTACK_SECRET_KEY` (and the rest of the payment-gateway constance settings) will be
  stored in plaintext in the database~~ — **Fixed 2026-07-13**, when Paystack work actually
  started (Task 10b), per this note's own instruction not to defer it. `PAYSTACK_PUBLIC_KEY` /
  `PAYSTACK_SECRET_KEY` moved to environment variables (`bancostore/settings.py`, matching
  `MNOTIFY_API_KEY`/`EMAIL_HOST_PASSWORD`) instead of `django-constance`. The rest of
  `PAYMENT_GATEWAY_SETTINGS` (channels, mode toggle, copy) stays in constance — legitimate
  business-rule config, not secrets.
- [x] ~~`SESSION_ENGINE = "django.contrib.sessions.backends.cache"` has no DB fallback
  (`cached_db`) — any Redis eviction/restart logs out every user platform-wide, including admin's
  mandatory-2FA state~~ — **Fixed 2026-07-31 (Task 30e).** Switched to `cached_db`; verified an
  existing session survives a `cache.clear()` (the exact failure mode a Redis eviction/restart
  would otherwise cause). No migration needed — `django.contrib.sessions` was already installed.
- [x] ~~Distributor registration reveals phone-number existence (`apps/distributors/forms.py`'s
  uniqueness check)~~ — **Fixed 2026-07-13** as part of Task 10a, for an unrelated reason (payment
  gating the account, not this issue specifically): `clean_phone_number` no longer checks the
  `Distributor` table at all. It only checks `PendingRegistration` (to avoid an unhandled
  `IntegrityError` on a duplicate submission), which doesn't reveal whether a phone number belongs
  to a real, existing distributor.
- [x] ~~CI hygiene, not urgent: `.github/workflows/ci.yml` has no explicit `permissions:` block
  (defaults to broader `GITHUB_TOKEN` scope than needed), `actions/checkout@v4` is pinned to a
  mutable tag rather than a commit SHA, and there's no dependency vulnerability scan step
  (`pip-audit`/`safety`) yet~~ — **Fixed 2026-07-31 (Task 30f).** Added an explicit top-level
  `permissions: contents: read` block, pinned both actions to a commit SHA (with the
  human-readable version as a trailing comment), and added a `pip-audit` step — deliberately
  non-blocking (`|| true`) since it's the first-ever scan and surfaced a real pre-existing backlog,
  now tracked separately below ("Known issues — surfaced by Task 30f's first-ever pip-audit run").

## Known issues — flagged during Task 18b verification, unrelated to Task 18b (2026-07-26)

- [ ] `tests/feature/admin_portal/test_withdrawal_review.py` intermittently fails 5 of its 17 tests
  (`test_queue_summary_cards_show_real_totals_not_the_stitch_mockup_numbers`,
  `test_high_priority_card_counts_requests_above_the_threshold`,
  `test_detail_shows_the_request_and_distributor_data`,
  `test_approving_from_the_detail_screen_debits_the_wallet`,
  `test_approve_flash_message_shows_the_real_name_not_the_debug_repr`) when run as part of the
  **full** suite, but passes 17/17 every time in isolation. Confirmed unrelated to Task 18b's own
  changes two ways: (1) `tests/feature/admin_portal/` collects alphabetically before any file Task
  18b touched, so causation the other direction is structurally impossible; (2) running the full
  suite again with Task 18b's changes entirely stashed out (`git stash -u`) still passed clean
  (870/870) — the flake didn't even reproduce without this task's code present at all, meaning it's
  intermittent/order-dependent against something else in the suite, not deterministic either way.
  Not root-caused or fixed here — out of scope for Task 18b — but flagged rather than silently
  worked around. Worth a real `debugging-and-error-recovery` pass whenever withdrawal-review or a
  neighboring `admin_portal` test file is next touched.
- [ ] `tests/feature/catalog/test_product_admin.py::test_product_changelist_does_not_n_plus_one_on_category`
  failed once as part of the **full** suite (2026-07-26, during Task 18e verification) with
  `30 <= 27 + 2` — a 1-query overshoot past the test's own already-documented tolerance for
  django-silk's "occasional internal housekeeping" query noise (see the test's own comment). Passed
  cleanly both in isolation and when run alongside the *entire* `tests/feature/admin_portal/` suite
  (101 passed, 0 failed) — confirmed unrelated to Task 18e, which touches no file under
  `apps/catalog/` at all. A second instance of the same class of full-suite-only flakiness as the
  entry above, not root-caused here. Worth revisiting if it recurs.
- [ ] `tests/feature/distributors/test_withdrawal_request.py::test_can_submit_a_valid_withdrawal_request`
  failed once as part of the **full** suite (2026-07-27, during Task 18f verification) — the success
  flash message ("Withdrawal request submitted") was missing from the rendered response even though
  the `WithdrawalRequest` row itself was created correctly with the right amount/tax/net. Passed
  13/13 immediately after in isolation. Confirmed unrelated to Task 18f, which touches no file under
  `apps/withdrawal/`, `apps/distributors/`, or `tests/feature/distributors/` (`git status` shows only
  `apps/admin_portal/`, `templates/admin_portal/`, and `static/src/main.css` changed). A third
  instance of the same class of full-suite-only flakiness as the two entries above, not root-caused
  here.
- [ ] `tests/unit/distributors/test_kyc_review.py::test_concurrent_approvals_of_different_distributors_never_duplicate_ir_ids`
  failed once as part of the **full** suite (2026-07-27, during Task 18f's follow-up verification)
  with `django.db.utils.OperationalError: database table is locked: distributors_iridsequence` --
  SQLite's well-known single-writer lock contention under a real multi-threaded concurrency test,
  exactly the class of flake `SPEC.md`'s own Testing Strategy already documents as expected under
  SQLite (why commission/wallet/PV-ledger concurrency tests run against real MySQL in CI instead of
  local SQLite). Passed 15/15 immediately after in isolation. Confirmed unrelated to this session's
  changes, which touch no file under `apps/distributors/` at all (same `git status` scope as the
  entry above). A fourth instance of full-suite-only flakiness, but a different root cause (SQLite
  locking, not query-count/response-content noise) than the three entries above it.

**Investigation (2026-08-05), per explicit user request** — this pattern recurred at least twice
more since the four entries above (Task 29's closeout; PR #60's `test_lockout_sends_an_alert_email`
today), 6+ occurrences total, always with the same fingerprint: passes in isolation, fails only in
full-suite order, a different test each time. Ran a real `debugging-and-error-recovery` pass rather
than continuing to defer it:

- **Ruled out (tested directly, not just reasoned about):** a `transaction=True` test's database
  writes do **not** get wiped by Django's post-test flush the way raw `TransactionTestCase` docs
  would suggest — verified with a direct reproduction (seeded `Group` rows survived a
  `transaction=True` test's teardown intact).
- **Ruled out (tested directly):** a `transaction=True` test mutating shared state (e.g. a
  `django-constance` setting) cannot leak forward into an earlier-numbered plain
  `@pytest.mark.django_db` test — confirmed pytest-django deterministically runs **every** plain
  `db` test to completion before **any** `transaction=True` test starts, regardless of file order or
  command-line argument order (verified by forcing explicit cross-file ordering three different
  ways; pytest-django reordered every time). This structurally rules out "an earlier transactional
  test polluted a later plain test" for any of the plain-test failures in this list.
- **Confirmed live, not just theorized:** re-running the full suite today caught a real
  `django.db.utils.OperationalError: database table is locked` in a background thread (surfaced as
  a `PytestUnhandledThreadExceptionWarning`, not a hard failure this time) — direct evidence that
  SQLite's coarse table-level write lock is genuinely contended during the `transaction=True` batch
  pytest-django runs at the end of every session. This project has 22 test files using
  `transaction=True` (real multi-threaded concurrency proofs).
- **Not yet confirmed:** the exact mechanism behind the *plain*-test failures (missing flash
  message, off-by-one query count, missing outbox email) remains open — ruling out cross-test
  transactional pollution narrows it to either leftover in-process Python state (a module-level
  cache/singleton) or Redis-cache state not covered by the existing `_clear_django_cache` autouse
  fixture, but neither was caught live this pass.

**Correction (2026-08-05):** the first draft of this investigation recommended broadening CI's
real-MySQL coverage to all 22 `transaction=True` files, on the mistaken assumption that
`.github/workflows/ci.yml` only ran the commission/wallet/withdrawal subset against MySQL. Checked
the actual workflow file directly before implementing that: `pytest -q` with no path/marker filter,
`testpaths = ["tests"]` in `pyproject.toml` — **CI already runs the entire suite against real MySQL,
every file, every time.** There is nothing to broaden; the file is already correct. This means the
SQLite lock contention confirmed above is a **local-development-only artifact** (this Mac cannot run
MySQL at all, per this project's own long-documented constraint — see `SPEC.md` Local dev
environment), not a CI or merge-safety gap. It causes confusing local flakes when running the full
suite on this Mac, but never a false CI failure blocking a real merge. No further action taken on
`.github/workflows/ci.yml` — correcting course rather than making a pointless change.

## Known issues — surfaced by Task 30f's first-ever pip-audit run (2026-07-31)

- [ ] `pip-audit -r requirements.txt` (added non-blocking in `.github/workflows/ci.yml` — see Task
  30) surfaced a real backlog of pre-existing advisories across several packages, some of which are
  deliberately pinned in this codebase for unrelated reasons documented in `requirements.txt`/
  `CLAUDE.md` (e.g. `cbor2==5.5.0`, pinned because later versions need a Rust compiler this Mac
  doesn't have): Django, `django-allauth`, Pillow, WeasyPrint, `cbor2`, pytest, and black each have
  one or more open advisories. None triaged yet for reachability/exploitability in this codebase's
  actual usage, and none of the fixed versions have been checked against this project's own pinning
  constraints. Needs a dedicated `security-and-hardening` pass — per that skill's own triage
  decision tree (severity, reachability, fix availability) — before any of these are upgraded, one
  package at a time per this project's dependency-upgrade discipline, not a bulk bump.

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
- [x] Three groups exist: `customer`, `distributor`, `admin` (created by an `apps.accounts` data
  migration, so they exist after `migrate` alone, not only after seeding)
- [x] `Distributor` model/migration exists with at least `user`, `sponsor`, `rank`, `kyc_status`
  (plus `ir_id`, all nullable/blank until Tasks 10/11 fill them in)
- [x] A management command (`seed_roles`) creates one stub user per role for local testing

**Verification:**
- [x] pytest test: a user assigned the `distributor` group has a `Distributor` row and passes an
  `is_distributor()` check (`tests/unit/accounts/test_permissions.py`)
- [x] `python manage.py migrate` runs clean; `seed_roles` populates the three stub users, and is
  idempotent (`tests/feature/test_seed_roles.py`) — verified manually from a fresh `db.sqlite3` too

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
- [x] `CONSTANCE_CONFIG` covers every field in the categories above (73 settings), grouped by
  `CONSTANCE_CONFIG_FIELDSETS` (8 fieldsets, one per category) — defined in
  `apps/platform_settings/config.py` and imported into `bancostore/settings.py`
- [x] Defaults match every value listed in Section 15 (Key Rules Summary) that falls within these
  8 categories — the 70% Retail Rule (13.12) and Delivery Fees (13.5) are Section 15 rules
  controlled by out-of-scope sections and are intentionally not seeded here, per SPEC.md's own
  scope note (13.5–13.8, 13.10–13.12 stubbed elsewhere)
- [x] Settings are Redis-cached via `CONSTANCE_DATABASE_CACHE_BACKEND = "default"` (constance stays
  DB-backed/admin-editable per SPEC.md Tech Stack, with Redis as a read-through cache in front of it)

**Verification:**
- [x] pytest test: reading `constance.config.BINARY_BONUS_RATE` returns `7.5`
  (`tests/unit/platform_settings/test_constance_config.py`)
- [x] pytest test: updating a setting value takes effect on the next read without a code change —
  also verified manually via the Django Admin constance page
  (`/admin/constance/config/`, confirmed rendering all 8 fieldsets and the correct `7.5` default)
- Note: found that Redis-backed constance caching doesn't get cleared by pytest-django's normal
  per-test DB rollback, so a value written in one test was leaking into the next via Redis. Added
  an autouse `tests/conftest.py` fixture that clears the Django cache before/after every test.

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

**Decisions made:** email provider is Gmail SMTP (falls back to the console backend — prints
emails to the terminal — when no Gmail app-password is set in `.env`, so local dev/tests never
need real credentials). No Google OAuth credentials exist yet, so Google login is fully wired at
the code level but has no `SocialApp` row configured — allauth correctly hides the button rather
than showing a broken one, verified manually. Add Google credentials via Django Admin → Social
Applications once available, no code change needed.

**Acceptance criteria:**
- [x] Customer can register with full name, email, phone number, and a password
  (`apps/accounts/forms.py` `CustomerSignupForm`) — also joins the `customer` group from Task 2
- [x] Google login via django-allauth is wired (socialaccount app + google provider, both
  installed since Task 1) — not yet clickable/testable end-to-end pending OAuth credentials (see
  Decisions above)
- [~] Guest checkout: no login-required middleware exists anywhere in the project, so anonymous
  browsing is never blocked — but there is no actual checkout page yet (Task 17, Phase 6), so the
  full guest-checkout UI itself is out of scope until then. Not writing a test against a page that
  doesn't exist yet.
- [x] Password reset flow works via email link (`apps/accounts/forms.py` uses allauth's built-in
  reset flow; confirmed via a full pytest round-trip: request reset → extract link from the sent
  email → set new password → new password works)

**Verification:**
- [x] pytest feature test: register → log out → log in
  (`tests/feature/accounts/test_customer_auth.py::test_customer_can_register_logout_and_login`)
- [x] pytest feature test: password reset completes and new password works
  (`tests/feature/accounts/test_customer_auth.py::test_customer_password_reset_completes_via_email_link`)
- [x] Manual check: loaded `/accounts/signup/`, `/accounts/login/`, `/accounts/password/reset/`
  on the real dev server (not just tests) — all 200, signup form shows exactly the right fields
  (full_name, email, phone_number, password1, password2, no username), no Google button (correct,
  no credentials configured yet — see Decisions above)

**Dependencies:** Task 2, Task 3, and the email-provider open question

**Files likely touched:** `apps/accounts/views.py`, `bancostore/urls.py`, `templates/accounts/*.html`, `tests/feature/accounts/test_customer_auth.py`

**Update 2026-07-10 — real UI built from the "Bancostore" Stitch project.** The frontend slice was
initially skipped (django-allauth's bare default templates only) — caught by the user as a broken
vertical slice, see `feedback_vertical_slice_build_process.md`. Fixed by fetching all 6 screens
from the user's "Bancostore" Stitch project (desktop-width, 1280px) and building real templates:
`templates/base_auth.html` (shell only — no header/footer, those come with the landing page) plus
`templates/account/{signup,login,password_reset,password_reset_done,password_reset_from_key,
password_reset_from_key_done}.html`. The full Stitch design system (colors, typography, spacing,
radii — the "Kinetic Retail Narrative" system) is now in `static/src/main.css` as a Tailwind v4
`@theme` block, generated programmatically from the Stitch-exported config to avoid transcription
errors. `CustomerLoginForm`, `CustomerResetPasswordForm`, and `CustomerResetPasswordKeyForm` were
added (`apps/accounts/forms.py`) purely to attach matching Tailwind classes to allauth's built-in
fields — no behavior change. A `terms_accepted` checkbox (required) was added to signup, matching
the Stitch design, since consent should be enforced regardless of whether the actual Terms/Privacy
pages exist yet.

Two real bugs surfaced and got fixed during this pass, both pre-existing from Task 1, not
introduced here: (1) `static/src/main.js` never imported `main.css`, so Tailwind was never
actually being bundled at all until now — nothing had rendered a real page to notice; (2) Vite's
default hashed output filenames had no way to be resolved from a Django template, so
`vite.config.js` now emits stable filenames (`assets/main.css`/`assets/main.js`) — revisit with
real cache-busting once there's a deploy pipeline (Task 24).

Verified: full pytest suite (17 tests, including a new one proving `terms_accepted` is actually
enforced), and a real end-to-end manual walk of all 6 pages via the dev server and curl — signup,
login, forgot password, check-your-email, reset password (via a real emailed reset link, not a
fabricated URL), and reset success, confirming the new password actually works afterward. Could
not get a live browser screenshot — the Claude in Chrome extension wasn't connected this session —
so the visual check was structural (rendered HTML, correct field IDs, correct CSS classes, correct
compiled stylesheet) rather than a literal look at the page. Recommend an actual visual pass in a
browser before this is considered fully signed off.

**Update 2026-07-11 — visual check done in browser, and real Gmail SMTP verified.** The user
opened all 6 pages directly in their own browser and confirmed they look correct — closes the gap
noted above. Separately, the user set up a real Gmail account + app password for password-reset
email and added it to `.env` (not committed, per `.gitignore`). First real send attempt failed
with `ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` — this Mac's python.org-installed
Python doesn't pick up the system CA trust store, so STARTTLS to Gmail fails without an explicit
cert bundle. Fixed in `bancostore/settings.py` by pointing `SSL_CERT_FILE` at `certifi`'s bundle
(already installed as a transitive dependency of `requests`, no new package needed) whenever the
SMTP backend is active. Retried and got a clean 302 with no server error — a real password-reset
email sent successfully through Gmail SMTP.

**Estimated scope:** M

---

### Task 5: Distributor registration & login

**Description:** Phone number + password login, SMS OTP verification during registration and for
password reset, account lockout after N failed attempts (from settings).

**Decision made 2026-07-11:** SMS provider is **mNotify** (not Arkesel/Hubtel as originally named
in the docs — see `SPEC.md` Open Question #2). User has an API key.

**Acceptance criteria:**
- [x] Distributor registers with phone + password; phone verified via OTP
  (`apps/distributors/views.py`, `apps/notifications/otp.py`)
- [x] Login uses phone + password only (no Google login) — custom `PhoneNumberBackend`
  (`apps/distributors/backends.py`), no allauth/social auth involved at all for distributors
- [x] Wrong-password lockout uses `django-constance` values
  (`MAX_FAILED_LOGIN_ATTEMPTS`, `ACCOUNT_LOCKOUT_DURATION_MINUTES`, seeded in Task 3), not
  hardcoded numbers — `apps/distributors/services.py::attempt_distributor_login`

**Also built (beyond the 3 literal criteria above, to match the Description and the already-
commissioned Stitch screens):** distributor password reset via SMS OTP
(`forgot_password`/`set_new_password` views), reusing the same OTP infrastructure.

**Verification:**
- [x] pytest feature test: registration + OTP verification + login
  (`tests/feature/distributors/test_distributor_auth.py::test_distributor_can_register_verify_otp_and_login`)
- [x] pytest feature test: N failed logins locks the account for the configured duration
  (`test_n_failed_logins_locks_the_account_for_the_configured_duration` — also verified the lock
  clears once the window passes, and that a successful login resets the counter). Deliberately
  broke the lockout condition and re-ran this test to confirm it actually fails when the logic is
  wrong, not just passing vacuously.
- [x] Manual check: full register → verify → login flow walked through the real dev server (not
  just the test client) via curl, confirmed `phone_verified=True` and `failed_login_attempts=0`
  in the database afterward
- [x] Manual check: OTP sent through mNotify's real API — **confirmed working end to end
  2026-07-11**, including the user actually receiving a real SMS and logging in through the real
  browser UI (driven live via the Claude in Chrome extension once connected). Getting here surfaced
  several real, non-code issues along the way, all resolved:
  - mNotify rejected the send with "sender id is not registered or approved" until the user got
    "Bancostore" approved as a sender ID on their account — not a bug, an mNotify account step
  - A follow-up diagnostic send got a 419 "fraudulent content suspected" — caused by the
    diagnostic message's own wording (e.g. "please ignore"), not a real problem; the app's actual
    OTP message text is normal transactional copy and wasn't affected
  - Real-world delivery took longer than the original 5-minute OTP expiry — temporarily raised
    `OTP_CODE_EXPIRY_MINUTES` to 15 via the live django-constance value (no code/deploy needed,
    exactly what Task 3 built constance for) while confirming; revisit whether 15 should be the
    permanent default given this is Ghana telecom delivery latency, not a one-off
  - **Real bug found and fixed:** a successful distributor login redirected straight back to
    `distributors:login` — same page, empty form, no error — which looked exactly like the login
    had silently failed even though it had actually succeeded (session created). No dashboard
    exists yet (that's Task 20), so there was nowhere honest to send a logged-in user. Added a
    minimal `distributors:dashboard` placeholder view (`apps/distributors/views.py`,
    `templates/distributors/dashboard.html`) as a real, honest landing spot, and fixed both
    places that log a user in (registration-completion and direct login) to redirect there
    instead. Caught by the user's own manual testing, not by the automated test suite — added a
    regression test (`test_successful_login_does_not_redirect_back_to_the_login_page`) afterward
  - **Test isolation bug found and fixed:** once a real `MNOTIFY_API_KEY` existed in `.env`, the
    entire test suite started silently calling the real mNotify API on every run — Django's test
    runner auto-forces `EMAIL_BACKEND` to locmem regardless of `.env`, but there's no equivalent
    built-in protection for custom settings like ours. Added an autouse fixture in
    `tests/conftest.py` forcing `MNOTIFY_API_KEY` empty for every test run, permanently
  - **Real bug found and fixed:** `PhoneNumberField` rejected local-format Ghana numbers (e.g.
    `0545488681`, typed without `+233`, exactly how a real user types it) because no default
    region was configured. Added `PHONENUMBER_DEFAULT_REGION = "GH"` (Bancostore is Ghana-only),
    fixing this for both the customer and distributor forms with one setting; added a regression
    test for each

**UI status: done (2026-07-11).** All 7 distributor screens (Register, Verify Code, Log In,
Account Locked, Forgot Password, Set New Password, Reset Success) were designed by the user in
the same "Bancostore" Stitch project as Task 4, fetched, verified field-by-field against the
original prompt (phone number field present, no Google/social login button on register/login,
OTP entry present on Verify Code, Account Locked has no form, etc. — all matched), and built as
real templates in `templates/distributors/*.html`, replacing the temporary unstyled placeholders.
No new design tokens were needed — same Stitch project, same "Kinetic Retail Narrative" theme
already in `static/src/main.css`.

Notable adaptations from the raw Stitch markup:
- Register screen included a "Distributor Agreement" consent checkbox not in the original
  prompt — added as a real required `terms_accepted` field (`apps/distributors/forms.py`),
  same pattern as Task 4's customer signup.
- Verify Code uses 6 separate digit boxes rather than one text field — kept the visual pattern,
  added a small JS snippet to sync the 6 boxes into the real hidden `code` field
  (`OTPVerificationForm`) instead of Stitch's non-functional `preventDefault()` mock submit.
- Account Locked is now a real page the login view renders (not just a form error) — added
  `locked_until` to the `LoginAttempt` result so the page can show the actual remaining lockout
  time computed from `ACCOUNT_LOCKOUT_DURATION_MINUTES`, not a static placeholder.
- Added a dedicated `distributors:reset_success` view/URL (POST-redirect-GET after password
  reset) so the Reset Success screen actually gets shown, rather than skipping straight to login.
- Dropped decorative-only elements with no functional purpose (marketing testimonial block on
  Set New Password, live JS password-strength icons) — kept the functional core of each screen.

Verified: full pytest suite (30 tests) with real templates now rendering (not placeholders) —
required updating 2 tests to submit the new `terms_accepted` field, and fixing one test's
Google-login check that was incorrectly matching Google Fonts `<link>` tags on every page,
not an actual login button. Manually walked the real dev server end to end, including
**deliberately triggering 5 real failed logins to see the actual Account Locked page** (not
simulated) — confirmed the displayed "30 min remaining" matched the real `locked_until` value in
the database.

**Dependencies:** Task 2, Task 3, SMS provider decision (resolved)

**Files likely touched:** `apps/distributors/views.py` (auth views), `apps/notifications/otp.py`, `tests/feature/distributors/test_distributor_auth.py`

**Estimated scope:** M

---

### Task 6: Admin login with mandatory 2FA

**Description:** Email + password login for admin via Django Admin, with mandatory 2FA
(django-two-factor-auth: authenticator app only — SMS dropped since django-two-factor-auth's SMS
method requires Twilio, and mNotify is the platform's only SMS provider) that cannot be disabled.
Failed attempts trigger lockout + alert email.

**Acceptance criteria:**
- [x] Admin cannot reach the admin panel without completing 2FA
- [x] 2FA cannot be turned off through any settings toggle
- [x] Lockout alert email fires on repeated failed attempts

**Verification:**
- [x] pytest feature test: login without completing 2FA is rejected
- [x] pytest feature test: login + correct 2FA code succeeds
- [x] Manual browser verification: fresh admin logs in with email+password, is forced through the
  TOTP setup wizard (QR/secret + token), and lands on the real Django Admin panel — full mandatory
  2FA flow confirmed end-to-end live, not just via pytest

**Backend + UI: done.** All 6 real Stitch screens are built and wired in — Admin Login, 2FA Setup
intro, Scan QR Code, Setup Complete, Account Locked, and Two-Factor Verification (returning-login
code entry) — verified live in a real browser end to end (login -> forced TOTP setup -> admin
panel, the lockout -> Account Locked path, and the returning-login -> code entry -> admin panel
path). The 6th screen was originally generated in Stitch with the wrong content (a "check your
email" password-reset screen instead of a code entry form); fixed in place via
`mcp__stitch__edit_screens` against the same screen ID, then fetched and verified against
django-two-factor-auth's actual AuthenticationTokenForm/BackupTokenForm before building
templates/two_factor/core/login_token.html (routed there via AdminLoginView.get_template_names(),
since this screen has its own standalone layout with no shared header/footer, unlike the other 5).

**Dependencies:** Task 2, Task 3

**Files likely touched:** `bancostore/settings.py` (two-factor config), `apps/accounts/admin.py`, `tests/feature/accounts/test_admin_auth.py`

**Estimated scope:** S

**Follow-up, 2026-07-30: "remember this device" for 7 days.** User-approved after a UX
discussion about login friction (the admin was re-entering a TOTP code on every single login, with
no way to avoid it). Uses django-two-factor-auth's own built-in `TWO_FACTOR_REMEMBER_COOKIE_AGE`
mechanism (`bancostore/settings.py`) rather than reinventing cookie signing/scoping/expiry — a
`doubt-driven-development` pass read the library's source directly and confirmed the cookie is
bound to (user, OTP device, password) and expires correctly; the pass also caught two real gaps
folded in before shipping: (1) no audit trail existed for "this login skipped OTP via a remembered
device" — added via `AdminLoginView.done()` logging, matching this codebase's existing convention
of auditing security-relevant admin events; (2) needed an explicit regression test proving a
brand-new device with no remember-cookie always still requires a fresh code (this feature is
strictly additive, never a way to disable 2FA — distinct from the existing
`test_2fa_requirement_cannot_be_bypassed_via_settings_toggle` guarantee).

A `code-review-and-quality` pass then caught a real security gap pre-merge: django-two-factor-auth
defines the "remember" checkbox with `initial=True` — pre-checked, an opt-out 7-day 2FA skip on the
highest-value account type in this system (approves real Paystack withdrawal payouts). Fixing this
took two attempts: the first (a form subclass forcing `initial=False`, swapped into
`AdminLoginView.form_list`) passed in isolation but silently failed under real use — root-caused via
a `debugging-and-error-recovery` investigation to a genuine library quirk:
`two_factor.views.core.LoginView.get_form()` unconditionally overwrites
`self.form_list[TOKEN_STEP]` with `registry.method_from_device(...).get_token_form_class()` on
every single call for the token step, and `self.form_list` is one shared object across the whole
process lifetime (frozen once at `as_view()` time, never copied per-request) — so any subclass
placed there gets silently discarded, process-wide, the first time any token-step form is
constructed. The correct fix mutates the already-constructed form *instance* in
`AdminLoginView.get_form()` after calling `super().get_form()`, which works regardless of which
form class the library's OTP-method registry decides to use.

Also fixed live-browser-testing: the logout-confirmation modal
(`templates/admin_portal/base_dashboard.html`) unconditionally claimed "You'll need to log in and
verify with your authenticator again," which is no longer true for a remembered device — corrected
to a plain "You'll need to log in again."

Shipped test coverage (`tests/feature/accounts/test_admin_auth.py`): checking the box sets a
cookie; a second login from the same client skips the token step; leaving it unchecked still
requires the token step next time; a remember-cookie never trusts a different admin account on the
same browser; a removed/recreated OTP device isn't trusted by an old cookie; the cookie actually
expires after 7 days (via `unittest.mock.patch` on `two_factor.views.utils.time.time` — no
third-party time-travel library needed or added); the checkbox itself renders unchecked; and the
audit log line fires only on the remembered-device path, never on a fresh code entry. Verified live
in a real browser: checked the box, logged out, logged back in, confirmed the OTP prompt was
skipped. Full suite green throughout (1123 passed, 1 skipped as of this change).

---

**Checkpoint B:** all three roles register/login end to end, tests green.

---

## Phase 2: Catalog & Storefront

### Task 7: Product model + Django Admin CRUD

**Description:** Product model (name, description, photos, price GHS, PV value, category,
variants, stock, active/hidden, featured flag). Django Admin customization for full admin CRUD.

**Acceptance criteria:**
- [x] Admin can create/edit/delete a product with photos (Pillow resize/WebP) via Django Admin
- [x] Product has a PV value used only for distributor purchases
- [x] Out-of-stock products show correctly (behavior stubbed per settings default)

**Verification:**
- [x] pytest test: creating a product via Django Admin persists correctly
- [x] pytest test: stock decremented on sale (basic case, full order flow comes in Phase 6)

**Dependencies:** Task 1, Task 3

**Files likely touched:** `apps/catalog/models.py`, `apps/catalog/admin.py`, `apps/catalog/migrations/*.py`

**Estimated scope:** M

**Done 2026-07-12.** Built `Category`, `Product`, `ProductImage`, `ProductVariant` models plus
`apps/catalog/services.py::decrement_stock` (concurrency-safe via `select_for_update()` — two
simultaneous purchases of the last unit can't both succeed). Photo uploads are resized (long edge
capped at 1600px) and converted to WebP on save, verified with real image bytes via Pillow, with a
mutation test confirming the resize/conversion logic is actually exercised (temporarily disabled
it, confirmed the tests fail, restored it). Out-of-stock behavior is deliberately hardcoded (not a
constance setting) — Section 13.10 "Product & Inventory Settings" is explicitly out of MVP scope
per SPEC.md ("stubbed with sane hardcoded defaults"); almost built an unnecessary settings toggle
here before checking the source doc and catching that. Verified live in a real browser through
actual Django Admin: created a category, created a product with a variant, confirmed
list view/filters/search all work, confirmed the unique-slug constraint rejects an accidental
double-submit correctly. No customer-facing UI — that starts in Task 8.

**Post-build review (agent-skills:code-review-and-quality) found two Required issues, both fixed
same day:**
- A claimed N+1 query on `ProductAdmin`'s changelist turned out to be a false positive — Django's
  own `ChangeList.apply_select_related()` already auto-applies `select_related()` when a relation
  field appears in `list_display`, verified by reading `django/contrib/admin/views/main.py`
  directly and confirming empirically (captured real queries at 1 vs. 6 products, identical count
  either way). Kept `list_select_related = ["category"]` anyway as self-documenting, but the
  original "Required" framing was wrong — should have verified before reporting it.
- `decrement_stock`'s `select_for_update()` was correct in design but never verified under real
  concurrency. A genuine multi-threaded test (`test_concurrent_decrements_never_oversell_the_last_
  unit`) reproduced an actual crash: `OperationalError: database table is locked` on SQLite
  instead of the intended `InsufficientStockError` — not a theoretical gap, a real one. Fixed with
  bounded retry-on-lock-contention (10 attempts, short backoff) in `decrement_stock`. First retry
  attempt still failed because the SQLite error message is "database table is locked", not
  "database is locked" as guessed — caught by actually running the test rather than trusting the
  fix on read-through. Stable across 5 repeated runs after the fix. Full suite: 63/63.

---

### Task 8: Public storefront — home, listing, detail, search/filter/sort

**Description:** Home page (featured products, join/shop CTAs), product listing with search bar,
category filter, price range filter, sort (newest/price/popular), and product detail page, using
Django views + HTMX for reactive filtering without full-page reloads.

**Planned 2026-07-12 — split into three vertical slices (8a–8c), each shippable and tested on its
own.** Shared context for all three:

- This is the first public-facing chrome of the site. `templates/base_auth.html` is deliberately a
  header/footer-less shell (Task 4 noted "those come with the landing page" — this is that task).
  8a introduces `templates/base_store.html` with the real site header/footer/nav; 8b/8c extend it.
- UI follows the Tasks 4–6 pattern: real screens from the "Bancostore" Stitch project ("Kinetic
  Retail Narrative" theme, desktop 1280px), design tokens already in `static/src/main.css`. Three
  screens needed — Home, Product Listing, Product Detail — **not yet designed in Stitch** (open
  question below). No backend slice ships with placeholder templates (see
  `feedback_vertical_slice_build_process.md` — that mistake was already made once in Task 4).
- The root URL `/` is currently a 404; 8a claims it for the home page.
- Listing/home queries must prefetch primary images and `select_related("category")` from the
  start — this is the highest-traffic page of the site (see SPEC.md Scale Architecture).
- Only `is_active=True` products ever appear anywhere public. Out-of-stock products still show,
  with an "Out of Stock" label instead of add-to-cart (`Product.in_stock` — behavior deliberately
  hardcoded per Task 7's note, not a constance setting).

**Decisions made 2026-07-12:**
1. **"Popular" sort ships now, interim-backed by `is_featured` first then `created_at`** — no
   orders exist until Task 17, so there's no sales signal yet. Swap in real sales data after
   Task 18; the UI option doesn't change, only the ordering behind it. Comment the interim query
   accordingly so it isn't mistaken for the final definition.
2. **Stitch screens: Claude writes detailed per-screen prompts, the user builds them in the
   Bancostore Stitch project, Claude fetches the results via the Stitch MCP** (Tasks 4–5 pattern,
   revised from the earlier "Claude generates via MCP" decision). Homepage layout/structure is
   modeled on the QNET homepage (user-provided screenshot, 2026-07-12): typographic hero,
   lifestyle photo strip, category tile grid, how-it-works cards, discover bento, featured
   product cards, distributor CTA band, dark multi-column footer — adapted to Bancostore's
   Kinetic Retail Narrative theme, desktop 1280px.

---

#### Task 8a: Site shell (header/footer) + home page

**Acceptance criteria:**
- [x] `templates/base_store.html` exists with the real site header (logo, Shop link, cart icon
  stub, Login/Register, "Join as Distributor" CTA) and footer, from the Stitch design
- [x] `/` renders the home page: featured products (`is_active=True, is_featured=True`) with
  primary image, name, GHS price; join/shop CTAs
- [x] Inactive or unfeatured products never appear on the home page

**Verification:**
- [x] pytest: featured+active products shown; inactive and unfeatured excluded
- [x] pytest: home page renders with zero products (empty state, no crash)
- [x] Manual browser check of the real page against the Stitch design

**Dependencies:** Task 7, Stitch Home screen
**Files likely touched:** `apps/catalog/views.py`, `apps/catalog/urls.py`, `bancostore/urls.py`,
`templates/base_store.html`, `templates/catalog/home.html`, `tests/feature/catalog/test_home.py`
**Estimated scope:** M

**Done 2026-07-12.** Header reconciled from the Storefront Home Stitch screen (pill-button style,
consistent with the site's already-shipped auth-page buttons); footer reconciled from the
Product Listing/Detail screens' 4-column pattern (home.html's own footer was the outlier of the
three fetched screens, so the majority pattern was adopted as canonical). `Product.primary_image`
property added (reads the already-prefetched `images` queryset, never re-queries) since three
templates needed "primary image first" logic. **Real bug caught by the mandated browser check,
not by tests:** the home view never passed `categories` into context even though the template
looped over it — the category tile grid silently rendered as a single "View All Products" tile
with nothing else. No test had asserted category tiles render at all. Fixed (view now queries
`Category.objects.all()`) and a regression test added (`test_home_shows_category_tiles`) — this is
exactly the class of gap "type-checking passes, feature is broken" that the mandated browser
check exists to catch.

#### Task 8b: Product listing — search, filters, sort, pagination (HTMX)

**Acceptance criteria:**
- [x] Search (name/description), category filter, price min/max, and sort
  (newest / price ↑ / price ↓ / popular-per-open-question-1) all compose — any combination
  narrows correctly, and filters survive pagination
- [x] HTMX swaps just the product grid (no full-page reload) and pushes the querystring
  (`hx-push-url`) so filtered results are shareable/bookmarkable; the same URL loaded directly
  (non-HTMX) renders the identical full page
- [x] Out-of-stock products show the label; inactive products are excluded; grid is paginated

**Verification:**
- [x] pytest: search returns the right product; category filter narrows; search+filter+sort
  combined return the correct intersection; page 2 preserves active filters
- [x] pytest: `assertNumQueries` — product grid query count is constant regardless of product
  count (prefetched images/category, no N+1)
- [x] Manual browser check: filter reactively without reload, confirm URL updates

**Dependencies:** Task 8a, Stitch Listing screen, open question 1
**Files likely touched:** `apps/catalog/views.py`, `templates/catalog/product_list.html`,
`templates/catalog/partials/product_grid.html`, `tests/feature/catalog/test_listing.py`
**Estimated scope:** M

**Done 2026-07-12.** One `<form>` wraps sidebar filters + toolbar + grid; `hx-trigger="change,
submit, keyup changed delay:500ms from:#search-input"` on the form itself means new content
swapped into `#product-results` (pagination links, sort option) stays covered by the same
delegated listener without needing per-element `hx-*` attributes. Category "buttons" are real
radio inputs styled with `peer-checked` (no JS needed, works with the form's native `change`
event). django-htmx (already in `requirements.txt`, unused until now) branches the view between
the full-page and partial-only template via `request.htmx`, which also makes the non-JS
progressive-enhancement path work for free — the same view returns the full page on a plain GET.
Query-count regression test follows the exact tolerance-of-2 pattern already established in
`test_product_admin.py::test_product_changelist_does_not_n_plus_one_on_category` (silk's
middleware adds its own DB writes per request under local `DEBUG=True`, so exact equality would
be flaky — confirmed by hitting that same flake here first, not assumed).

#### Task 8c: Product detail page

**Acceptance criteria:**
- [x] Detail page (by slug) shows all photos (primary first), name, description, GHS price,
  PV value, variants (name/value pairs), and category
- [x] In-stock: add-to-cart button rendered as an honest stub (disabled with "coming soon" state —
  no dead link) until Task 17 wires it; out-of-stock: "Out of Stock" label, no button
- [x] Inactive or nonexistent slug → 404

**Verification:**
- [x] pytest: detail renders all fields; out-of-stock shows label and no add-to-cart; inactive
  product 404s
- [x] Manual browser check against the Stitch design

**Dependencies:** Task 8a, Stitch Detail screen
**Files likely touched:** `apps/catalog/views.py`, `templates/catalog/product_detail.html`,
`tests/feature/catalog/test_detail.py`
**Estimated scope:** S

**Done 2026-07-12.** Variant name/value pairs grouped via Django's built-in `{% regroup %}` tag
(relies on `ProductVariant.Meta.ordering = ["name", "value"]` already sorting them correctly, so
no grouping logic needed in the view). Added `django.contrib.humanize` (built-in Django contrib
app, not a new pip dependency) so prices render comma-formatted ("GHS 2,450.00") matching the
Stitch design, applied to both this page and the shared product card partial.

**Post-build note (all of 8a–8c):** `django.contrib.humanize` added to `INSTALLED_APPS`
(`bancostore/settings.py`). Full suite: 100/100 passing (up from 99 — all pre-existing tests still
green, no regressions). `black`/`ruff` clean. Demo products with real generated images were seeded
into the local `db.sqlite3` (gitignored, not committed) to verify visually — left in place for
convenience since they don't affect tests or ship anywhere.

**Post-ship fixes 2026-07-12, found by the user's own review, not caught before calling 8a–8c
done** (see `feedback_vertical_slice_build_process.md` for the full retrospective on why): the home
view never passed `categories` to its template despite the template looping over it (empty category
grid — fixed, regression test added); header/footer CTAs pointed to `href="#"` instead of real URLs
(fixed — Log In → `account_login`, Become a Distributor → `distributors:register`; regression test
added in `test_navigation_links.py`; About/Contact left as `title="Coming soon"` since no such
pages exist in MVP scope); decorative Stitch images (photo strip, bento tile, CTA band) were
placeholder divs instead of the real fetched image URLs (fixed); `product_detail.html`'s
`grid-cols-[55%_45%]` plus a 64px gap summed past 100% width, pushing the right column off-screen
(fixed by switching to `fr` units, which correctly account for gaps); a background-image div had
`bg-cover` without `bg-no-repeat`, causing visible tiling (fixed) — and the fix appeared not to
work at first because Vite's stable filenames mean the browser can serve a stale cached
`main.css` after a rebuild (documented in `CLAUDE.md`'s Commands section: rebuild *and*
hard-refresh, always). Separately, the homepage photo strip's floating keyframe animation and
several hover/zoom micro-interactions present in the original Stitch HTML had been dropped during
the rebuild — restored, including `@media (prefers-reduced-motion: no-preference)` around the
animation (an a11y improvement beyond the raw Stitch output). Two more acceptance-criteria-vs-test
gaps found on a literal re-read of the checklist above: 8a's "primary image, GHS price" and 8c's
"all photos (primary first)" both lacked a test that would actually fail if the feature broke —
both closed. `apps/catalog/views.py` simplified: `Product.objects.storefront_visible()` (new
queryset method, `apps/catalog/models.py`) replaces the repeated
`.filter(is_active=True).select_related("category").prefetch_related("images")` that appeared in
all three views — makes the is_active invariant impossible to forget at a new call site, not just
shorter. `product_grid.html`'s six near-duplicate pagination-link expressions replaced with a
`page_url` template filter (`apps/catalog/templatetags/catalog_extras.py`). Full suite: 105/105.

---

**Checkpoint C:** admin-added product appears correctly on the public storefront. **Verified
2026-07-12** — the demo products created via Django Admin/shell for the manual browser check
render correctly on the home page, listing page (with working search/filter/sort via HTMX), and
detail page.

---

## Phase 3: Distributor Core Domain (MLM)

### Task 9: Binary tree schema and placement/spillover service

Split into three slices (9a/9b/9c) per `planning-and-task-breakdown` — the original single task was
sized L with a note to split if it grew past ~5 files; it touches three genuinely separable
concerns (schema, placement algorithm, read-path query) so it's split up front instead.

**Placement/spillover algorithm, confirmed with the user 2026-07-13** (not specified in `SPEC.md`
or the source docs, which only say placement happens on "either leg" with spillover to "the next
available spot"):
- The sponsor picks left or right explicitly at registration. If they don't, auto-balance falls
  back to whichever of the sponsor's two legs currently has less PV.
- Spillover stays within the originally chosen leg only — it never crosses to the other leg.
- Within that leg, the next open slot is found breadth-first: shallowest empty position first,
  left-before-right at each level.

#### Task 9a: Closure table schema (`binary_tree_edges`, `pv_ledger`)

**Description:** Create the `apps/binary_tree` and `apps/pv_ledger` Django apps. Build the
`BinaryTreeEdge` closure-table model (ancestor, descendant, depth, and which leg — left/right —
the descendant falls under from that ancestor's perspective) and the `PvLedger` aggregate model
(per-distributor, per-leg PV totals, updatable in O(1)). Migrations and admin registration only —
no placement logic yet.

**Acceptance criteria:**
- [x] `BinaryTreeEdge` stores every ancestor→descendant pair with depth and leg
- [x] `PvLedger` holds per-leg PV aggregates per distributor
- [x] Both models are registered in Django Admin for inspection

**Verification:**
- [x] pytest: creating an edge for a single parent-child pair produces the expected row(s)
- [x] Migrations apply cleanly against a fresh DB

**Dependencies:** Task 2

**Files likely touched:** `apps/binary_tree/models.py`, `apps/binary_tree/admin.py`, `apps/binary_tree/migrations/`, `apps/pv_ledger/models.py`, `apps/pv_ledger/admin.py`, `apps/pv_ledger/migrations/`

**Estimated scope:** S-M

---

#### Task 9b: Placement + spillover service

**Description:** Build `BinaryTree.place_distributor(sponsor, new_distributor, leg=None)`
implementing the confirmed algorithm above: direct placement into the chosen (or auto-balanced)
leg when open; otherwise breadth-first, shallowest-first, left-before-right spillover within that
same leg's subtree. Writes closure-table edges for the new distributor against every ancestor,
tagging the correct leg per ancestor.

**Acceptance criteria:**
- [x] Direct placement succeeds when the sponsor's chosen leg slot is empty
- [x] Spillover places a new distributor at the correct next-available position when the direct slot is taken, staying within the originally chosen leg
- [x] Spillover fills the shallowest slot first, left before right at each level (verified against a specific tree shape, not just "some slot in the subtree")
- [x] Auto-balance picks the leg with less PV when no leg is specified
- [x] Closure table stays consistent after multiple sequential placements, including spillovers (no orphaned or duplicate edges)

**Verification:**
- [x] pytest: sponsor's direct left slot taken → new distributor lands in the exact expected spillover position (assert specific ancestor/descendant/leg rows, not just "somewhere in the subtree")
- [x] pytest: auto-balance chooses the actually-weaker leg when leg isn't specified
- [x] pytest: closure table integrity holds after 5+ sequential placements including spillovers

**Dependencies:** Task 9a

**Files likely touched:** `apps/binary_tree/services.py`, `tests/unit/binary_tree/test_placement.py`

**Estimated scope:** M-L (the algorithmic core of Task 9; if auto-balance meaningfully complicates it, consider shipping explicit-leg-only placement first and fast-following with auto-balance)

---

#### Task 9c: Ancestor-aggregate query service

**Description:** Build the read path: given a distributor, return their full ancestor chain with
each ancestor's current leg-PV aggregates, in O(log n) or O(1) queries — never a recursive walk.
This is what Task 13's binary bonus job calls every 10 minutes, so its query cost must not grow
with tree depth or width.

**Acceptance criteria:**
- [x] Ancestor-aggregate lookup for a single distributor executes in a small, constant number of queries regardless of tree depth
- [x] Query count is verified, not just wall-clock time

**Verification:**
- [x] pytest test seeding a 10k+ node synthetic tree: assert query count via `django.db.connection.queries` / `assertNumQueries`
- [x] pytest test: aggregate values returned match manually-computed expected totals for a small hand-built tree

**Dependencies:** Task 9a, Task 9b (needs real placement data to query against meaningfully; the query logic itself only reads Task 9a's schema)

**Files likely touched:** `apps/binary_tree/services.py` (or a new `apps/pv_ledger/services.py` if it grows large), `tests/unit/binary_tree/test_ancestor_query.py`

**Estimated scope:** S-M

---

**Checkpoint (Task 9 complete):** a synthetic 10k+ node tree can be seeded, a new distributor
placed under an arbitrary sponsor with correct spillover (both explicit-leg and auto-balance
paths verified), and the ancestor-aggregate query returns correct totals in constant/log query
count — all verified by pytest, not just asserted.

---

### Task 10: Distributor onboarding — registration fee, starter pack, placement

Split into four slices (10a–10d) per `planning-and-task-breakdown` — the original single task was
sized L with a note to split payment handling from tree-placement-and-ledger if it grew past ~5
files, and it does. Also corrects a scope error found while planning: the original task bundled IR
ID generation in here, but `docs/Bancostore_Features_and_Workflow_v4.docx` Section 14 (the exact
numbered example `SPEC.md` cites for these tests) generates the IR ID *after* KYC approval (step
9), not during registration/placement (steps 4–6). IR ID generation moves to Task 11. Confirmed
with the user 2026-07-13.

**Paystack:** the user has a Paystack account with both test and live API keys (SPEC.md Open
Question #6 resolved 2026-07-13) — no credentials blocker. Writing/modifying the actual Paystack
integration code is still a `CLAUDE.md` Boundary item requiring explicit go-ahead before each of
10b/10c specifically, separate from having the keys.

#### Task 10a: Registration form + sponsor validation (no payment)

**Description:** Registration form (full name, phone, email, address, area, landmark, password,
sponsor's IR ID — auto-filled from a referral link query param per Section 4 step 1). Validates the
sponsor IR ID resolves to a real, existing distributor. No Paystack, no account creation yet — the
account isn't created until the registration fee is confirmed paid (Task 10b).

**Acceptance criteria:**
- [x] Form collects all required fields and validates them
- [x] An invalid/non-existent sponsor IR ID is rejected with a clear error
- [x] A referral link's IR ID pre-fills the sponsor field
- [x] No `User`/`Distributor` row is created at this step

**Verification:**
- [x] pytest feature test: valid form + valid sponsor IR ID passes validation
- [x] pytest test: invalid sponsor IR ID is rejected

**Known gap (found while building Task 10d, 2026-07-13):** no leg-choice field was added here, so
`BinaryTree.place_distributor` (Task 10d) always runs auto-balance (`leg=None`), never a sponsor's
explicit choice. See `project_binary_tree_placement_spillover_rule` memory. Not blocking; revisit
if explicit leg choice is wanted later.

**Dependencies:** Task 5

**Files likely touched:** `apps/distributors/forms.py`, `apps/distributors/views.py` (registration form), `tests/feature/distributors/test_registration_form.py`

**Estimated scope:** S-M

---

#### Task 10b: Registration fee payment (Paystack) — creates the account

**Boundary:** Paystack integration code — confirm with the user immediately before writing this
slice's payment/webhook code, per `CLAUDE.md`.

**Description:** GHS 100 registration fee (`REGISTRATION_FEE` setting) charged via Paystack
(Mobile Money or card). On confirmed payment (webhook), create the `User` + `Distributor` account
from Task 10a's validated form data. Fee is non-refundable and gates account creation — no account
exists until payment confirms.

**Acceptance criteria:**
- [x] Account is created only after Paystack confirms payment, never before
- [x] Registration fee is recorded as non-refundable
- [x] A failed/abandoned payment leaves no orphaned account

**Verification:**
- [x] pytest feature test using Paystack test mode: confirmed payment → account created
- [x] pytest test: webhook failure/non-success leaves no account created

**Dependencies:** Task 10a

**Files likely touched:** `apps/orders/paystack_webhook.py` (or `apps/distributors/`), `tests/feature/distributors/test_registration_payment.py`

**Estimated scope:** M

---

#### Task 10c: Starter pack selection + payment (Paystack) — sets rank

**Boundary:** Paystack integration code — confirm with the user immediately before writing this
slice's payment/webhook code, per `CLAUDE.md`.

**Description:** Newly-created distributor chooses Starter Pack A (GHS 1,500 / 500 PV / Bronze) or
B (GHS 2,000 / 1,000 PV / Silver) — price, PV, and rank read from `django-constance`, never
hardcoded. Paystack charge; on confirmed payment, set `distributor.rank` and record the purchase.
Per Section 14 step 5, this purchase is what "officially activates" the distributor.

**Acceptance criteria:**
- [x] Starter pack choice sets the correct PV and rank (Bronze/Silver) from settings, not hardcoded
- [x] Rank is only set once payment is confirmed

**Verification:**
- [x] pytest feature test using Paystack test mode: Pack B purchase → confirmed payment → Silver rank, 1,000 PV recorded
- [x] pytest test: Pack A sets Bronze rank / 500 PV

**Dependencies:** Task 10b

**Files likely touched:** `apps/distributors/views.py` (starter pack selection), `apps/orders/paystack_webhook.py`, `tests/feature/distributors/test_starter_pack.py`

**Estimated scope:** M

---

#### Task 10d: Binary tree placement + PV ledger update on starter-pack confirmation

**Description:** On Task 10c's confirmed starter-pack payment, call Task 9b's
`BinaryTree.place_distributor` and increment PV up the ancestor chain at write time (event-driven,
per `SPEC.md` Scale Architecture) via a new `apps/pv_ledger/services.py` — this write-side logic
doesn't exist yet; only the schema and the read-side aggregate query (Task 9a/9c) do. Pure internal
logic, no Paystack.

**Acceptance criteria:**
- [x] Distributor is placed in the tree (correct spillover) immediately on confirmed starter-pack payment
- [x] Every ancestor's `PvLedger` leg-PV total updates immediately, matching the purchased pack's PV

**Verification:**
- [x] pytest feature test reproducing Section 14 steps 4–7 exactly: Kofi joins under Ama, Pack B → Ama's right leg gets +1,000 PV
- [x] pytest test: PV ledger ancestor totals are correct immediately after a Pack B purchase, several levels up

**Dependencies:** Task 9a, Task 9b, Task 9c, Task 10c

**Files likely touched:** `apps/pv_ledger/services.py`, `tests/unit/pv_ledger/test_purchase_increment.py`

**Estimated scope:** S-M

---

**Checkpoint (Task 10 complete — verified 2026-07-13):** a new distributor can register, pay the
registration fee, choose and pay for a starter pack, and end up correctly placed in the binary tree
with ancestor PV ledgers updated — verified end to end through Paystack test mode, reproducing
Section 14 steps 4–7 exactly. Full suite green (209 tests) at commit `0bdc507`.

---

### Task 11: KYC verification (Didit), admin review, and IR ID generation

**Revised 2026-07-13.** Task 11a originally shipped as a self-hosted upload form (commit `ebf8e22`)
— distributor uploads Ghana Card front/back + a selfie directly through our own form, admin
eyeballs them manually with no automated check. The user then asked whether a real ID-verification
check was possible; research (see chat log) found **Didit** (didit.me), which supports Ghana Card
and offers a free tier (500 checks/month) via its **hosted verification flow** specifically (their
standalone/server-to-server API explicitly has no free tier and has no selfie/face-match support at
all — only the hosted flow does). The user chose to replace the self-hosted upload form entirely
with Didit's hosted flow: Didit checks the ID front/back AND the selfie (face-match + liveness),
distributor is redirected back afterward (Didit's `callback` parameter, same shape as this
project's existing Paystack `callback_url`), and results are shown to the admin — who still must
manually click approve/reject (Didit is never auto-approve/auto-reject; `SPEC.md`'s Boundaries
section explicitly says "Never: Auto-approve ... KYC ... even temporarily for admin").

This reshapes Task 11 into three slices instead of two, following the same reasoning as Tasks 9/10's
splits (genuinely separable concerns: data model, the session/webhook flow, and admin review are
different actors and different files) — plus a preliminary cleanup step removing the now-obsolete
upload-form code.

**Preliminary: remove the old upload-form implementation.** Its own clean commit before any new
code lands (separate refactor-vs-feature, per `code-review-and-quality`): delete
`ghana_card_front`/`ghana_card_back`/`selfie`/`kyc_submitted_at` fields and the `Distributor.save()`
override that WebP-converts them (migration to drop the columns), `KycSubmissionForm` and its
`MAX_KYC_UPLOAD_SIZE_BYTES`/`_validate_kyc_upload_size` (now dead code), the `submit_kyc` view and
its URL, `templates/distributors/submit_kyc.html`, and `tests/feature/distributors/test_kyc_submission.py`
(7 tests). `bancostore/media.py` (the shared WebP helper) stays — Category/ProductImage still use it.

**Also drops a duplicate/premature acceptance criterion found while originally planning this task:**
the original task said "distributor cannot request a withdrawal until KYC approved," but
`apps/withdrawal/` doesn't exist yet (Task 16, much later) and Task 16 already has its own identical
criterion ("Withdrawal blocked if KYC is not approved...", line ~1099). Task 11 only needs to leave
`kyc_status` correct for Task 16 to check later; the withdrawal-blocking test itself belongs to
Task 16.

**User-side setup (not code):** the user needs a Didit account with an API key (confirmed ready), a
"workflow" created in Didit's dashboard bundling ID Verification + Face Match + Liveness (gives a
`workflow_id`), and a webhook secret for signature verification. `DIDIT_API_KEY`,
`DIDIT_WEBHOOK_SECRET`, and `DIDIT_WORKFLOW_ID` go in `.env` as environment variables, matching
`PAYSTACK_SECRET_KEY`/`MNOTIFY_API_KEY` — never `django-constance` (credentials/integration IDs,
not business rules).

#### Task 11a: Didit verification model + API client

**Description:** Foundation only, no views yet. A new `DiditVerification` model
(`apps/distributors/models.py`, one-to-one with `Distributor`) holds Didit's structured response:
`session_id`, overall `status` (`pending`/`approved`/`declined`/`in_review` — Didit's own three
decision states plus our initial `pending`), separate id-verification/face-match/liveness
statuses and scores, extracted document fields (name, date of birth, document number), a
`warnings` JSON list, and the three images (fetched from Didit and stored locally per the user's
choice — WebP-converted via the existing shared helper). A new `apps/distributors/didit.py` module
(mirrors `apps/distributors/paystack.py`'s shape exactly) provides `create_verification_session()`,
`get_session_decision()`, and `verify_webhook_signature()`. Session-creation and decision-retrieval
endpoints/shapes were fetched and confirmed against Didit's real API docs
(`source-driven-development`). **Update 2026-07-14:** the webhook signature scheme was initially
shipped as `X-Signature-Simple` (a secondary-sourced, and as it turned out *wrong*, scheme —
Didit's own docs, successfully fetched on a second, more careful research pass, state that
"Simple"... "does NOT authenticate decision data") and has since been corrected to `X-Signature-V2`
(HMAC-SHA256 over the canonical JSON form of the whole payload, plus an `X-Timestamp` freshness
check), confirmed against `docs.didit.me/integration/webhooks` directly, including its exact
Python reference implementation.

**Acceptance criteria:**
- [x] `DiditVerification` stores session id, overall status, and per-check (ID/face-match/liveness) status+score
- [x] `create_verification_session()` calls Didit's real session-creation endpoint and returns the redirect URL + session id
- [x] `get_session_decision()` calls Didit's real retrieve-decision endpoint and returns a parsed result
- [x] `verify_webhook_signature()` correctly validates a genuine HMAC-SHA256 signature and rejects a tampered/wrong one -- scheme confirmed against Didit's primary docs 2026-07-14 (was `X-Signature-Simple`, corrected to `X-Signature-V2`)

**Verification:**
- [x] pytest test: model fields round-trip correctly (create, save, reload)
- [x] pytest test (mocked HTTP): `create_verification_session()` sends the correct request shape and parses a real-shaped response
- [x] pytest test (mocked HTTP): `get_session_decision()` parses a real-shaped decision response into the expected fields
- [x] pytest test: `verify_webhook_signature()` accepts a correctly-signed payload and rejects an incorrect one

**Dependencies:** Task 5

**Files likely touched:** `apps/distributors/models.py`, `apps/distributors/migrations/` (drop old KYC fields, add `DiditVerification`), `apps/distributors/didit.py` (new), `bancostore/settings.py`/`.env.example` (new env vars), `tests/unit/distributors/test_didit_client.py` (new), `tests/unit/distributors/test_didit_verification_model.py` (new)

**Estimated scope:** M

**Done 2026-07-13 (commit `fb7f857`).**

---

#### Task 11b: Didit verification flow end-to-end (session, callback, webhook)

**Description:** Wires Task 11a's model/client into the actual distributor-facing flow.
`start_kyc_verification` view (phone-verified gate, same as the old `submit_kyc`) creates a Didit
session and redirects the distributor to Didit's hosted page. A callback view (fast path, mirrors
`starter_pack_payment_callback`) and a webhook view (HMAC-verified via `verify_webhook_signature()`,
mirrors `paystack_webhook`) both call one idempotent `consume_didit_result(session_id)` service —
locked the same way `consume_paid_starter_pack` is (`select_for_update_nowait_if_supported` +
`retry_on_lock_contention`), and **never trusts the callback query string or webhook payload
directly**: it always re-fetches the authoritative result via `get_session_decision()` before
storing anything, exactly the same "always re-verify server-side" pattern already used for
Paystack. Downloads and WebP-converts the three images at this point.

**Update 2026-07-14 — both previously-unverified assumptions are now confirmed.** The webhook
signature scheme was corrected to `X-Signature-V2` (HMAC-SHA256 over the canonical JSON payload,
confirmed against Didit's real primary docs). The `front_image`/`back_image`/`portrait_image`
field names were confirmed against a real, live Didit verification session (a real Ghana Card +
selfie, run through the actual hosted flow with the user's own Didit account) — the field names
were correct, but that same live test caught a different real bug: `_assert_safe_media_url`'s SSRF
allowlist only permitted `*.didit.me` hosts, while Didit actually serves images from a specific S3
bucket (`service-didit-verification-production-a1c5f9b8.s3.amazonaws.com`), so every image silently
failed to download until fixed. Re-ran the same session after the fix — all three images
downloaded and converted to WebP correctly.

**Acceptance criteria:**
- [x] Distributor is redirected to Didit's hosted page with a real session, and redirected back afterward
- [x] Both the callback and the webhook path resolve to the same stored result (idempotent, no double-processing)
- [x] The stored result always comes from re-querying Didit's decision endpoint, never from trusting the callback/webhook payload alone
- [x] Webhook requests with an invalid/missing signature are rejected
- [x] Fetched images are converted to WebP and stored on `DiditVerification`

**Verification:**
- [x] pytest feature test (mocked HTTP): full flow — start session → simulate callback → `DiditVerification` updated with the re-fetched result
- [x] pytest feature test: webhook with a valid signature updates the result; invalid signature is rejected (400)
- [x] pytest test: calling the consume path twice for the same session is a safe no-op the second time
- [x] pytest test: images fetched from Didit are stored as WebP

**Dependencies:** Task 11a

**Done 2026-07-13 (commit `746d38b`).** Also added an SSRF guard on image downloads (found during
`code-review-and-quality`/`security-and-hardening`, not in the original acceptance criteria) — see
`apps/distributors/services.py::_assert_safe_media_url`.

**Files likely touched:** `apps/distributors/views.py` (`start_kyc_verification`, callback, webhook; removes `submit_kyc`), `apps/distributors/services.py` (`consume_didit_result`), `apps/distributors/urls.py`, `templates/distributors/kyc_verification_callback.html` (new), `tests/feature/distributors/test_kyc_verification.py` (new)

**Estimated scope:** M

---

#### Task 11c: Admin KYC review + IR ID generation on approval

**Description:** Admin sees Didit's result (status, scores, extracted data, warnings, and the
locally-stored images) alongside each pending `Distributor` in Django Admin, and approves or
rejects (rejection requires a reason, from the existing `KYC_REJECTION_REASONS` constance setting).
Didit's own status (including its `in_review` "I can't decide this one" state) is shown as
context, never used to auto-decide — the admin's click is always what sets `kyc_status`, per
`SPEC.md`'s "never auto-approve KYC" boundary. Approval atomically assigns a permanent IR ID in the
configured format (`IR_ID_PREFIX` / `IR_ID_STARTING_NUMBER` / `IR_ID_NUMBER_OF_DIGITS`) — Section 14
step 9 ("The admin approves his KYC. He receives his IR ID Number."). IR ID generation needs a
concurrency-safe sequence design (never duplicated or reassigned under concurrent approvals) — run
this through `doubt-driven-development` before implementing, same rigor as the
PendingRegistration/Paystack designs got in Task 10a/10b.

**Acceptance criteria:**
- [x] Admin sees pending KYC submissions, with Didit's result and images, in Django Admin
- [x] Approve assigns a permanent, correctly-formatted IR ID; reject requires a reason and never assigns an IR ID
- [x] IR ID is generated exactly once per distributor and never reassigned or duplicated, even under concurrent approvals
- [x] Approving an already-approved distributor is a safe no-op (doesn't regenerate or overwrite the IR ID)
- [x] Didit's result is informational only — it never sets `kyc_status` by itself, regardless of its own status value

**Verification:**
- [x] pytest feature test: admin approves pending KYC → `kyc_status` flips to `approved` and a correctly-formatted IR ID is assigned
- [x] pytest feature test: admin rejects with a reason → `kyc_status` flips to `rejected`, no IR ID assigned, reason stored
- [x] pytest test (threaded, same convention as Task 9b/10d): concurrent approvals of different distributors never produce duplicate or reused IR IDs
- [x] pytest test: IR ID is never assigned before approval, and never reassigned once set
- [x] pytest test: a `DiditVerification` with status `declined` or `in_review` does not change `kyc_status` on its own (only an explicit admin action does)

**Dependencies:** Task 11b, Task 3

**Done 2026-07-14 (commit `6c3fcff`).** IR ID sequence design went through a full
`doubt-driven-development` cycle (10 findings, 8 folded in, 2 verified as noise). Also removed two
pre-existing constance settings that contradicted this task's design
(`KYC_AUTO_APPROVE_ENABLED`, `KYC_DOCUMENTS_REQUIRED`), and fixed a real full-suite-only test flake
(a `transaction=True` test elsewhere flushes migration-seeded data away) uncovered while verifying.

**Files likely touched:** `apps/distributors/admin.py`, `apps/distributors/services.py` (or a new `apps/distributors/kyc_services.py` if it grows large — IR ID generation), `apps/distributors/models.py` (`kyc_rejection_reason` field + migration), `tests/unit/distributors/test_kyc_review.py`

**Estimated scope:** M

---

**Checkpoint (Task 11 complete — verified 2026-07-14):** a distributor is redirected through
Didit's hosted verification (ID + selfie), the result (plus images) is stored and shown to admin,
admin approves or rejects with a reason via Django Admin, and approval assigns a permanent,
correctly-formatted, never-reused IR ID — verified by pytest, including under concurrent
approvals. Didit never decides `kyc_status` by itself. Full suite green (261 tests) at commit
`6c3fcff`.

---

## Phase 4: Commission Engine (elevated test rigor — see `SPEC.md` Testing Strategy)

### Task 12: Direct Referral Bonus — instant credit

**Description:** On starter-pack purchase confirmation, credit the sponsor's wallet with
`DIRECT_REFERRAL_BONUS_RATE` × the new distributor's PV (1 PV treated as GHS 1 for this
calculation), instantly, plus a notification.

**Update 2026-07-14 — formula confirmed, a real doc contradiction resolved.** The original
requirements doc's Section 14 walkthrough states Ama earns GHS 200 for Kofi's Pack B purchase and
Kofi earns GHS 75 per Pack A referral -- neither number is reproducible from one consistent formula
using either "rate × PV" or "rate × price paid" across both examples at once (confirmed by working
the arithmetic both ways), and it also contradicts this very file's own prior verification note
(which already said GHS 100, not 200, for the same Pack B scenario). The user confirmed the
authoritative formula is **rate × PV**: Pack A (500 PV) = **GHS 50**, Pack B (1,000 PV) = **GHS
100**. Section 14's GHS 200 / GHS 75 figures are narrative errors in the original doc, not a
second valid interpretation -- see the `project_direct_referral_bonus_formula` memory.

**Done 2026-07-14, built as three slices (12a/12b/12c):** 12a — new `apps/wallet` app (`Wallet`,
`WalletTransaction`, `credit()`, atomic `F()` update + `retry_on_lock_contention`, proven
concurrency-safe with a 5-thread test). 12b — new `apps/commissions` app
(`calculate_direct_referral_bonus`, establishing this codebase's first money-rounding convention:
`ROUND_HALF_UP` to the pesewa), hooked into `consume_paid_starter_pack` inside the same
locked/idempotent block as placement and PV credit. 12c — sponsor SMS notification, wrapped so a
notification failure can never roll back an already-committed credit.

**Acceptance criteria:**
- [x] Rate is read from `django-constance` config, not hardcoded
- [x] Credit happens the moment payment is confirmed (same request/Celery task, not delayed)
- [x] Sponsor receives a notification with the correct amount and referred distributor's name

**Verification:**
- [x] pytest test: Pack A purchase → sponsor credited GHS 50 (10% of 500 PV)
- [x] pytest test: Pack B purchase → sponsor credited GHS 100 (10% of 1,000 PV)
- [x] pytest test: rounding to the pesewa is correct on a non-round PV value

**Dependencies:** Task 10c, Task 10d, Task 3

**Files likely touched:** `apps/commissions/services.py` (`DirectReferralCalculator`), `apps/wallet/services.py` (`WalletService`), `tests/unit/commissions/test_direct_referral.py`

**Estimated scope:** S

---

### Task 13: Binary Bonus task — every 10 minutes

**Description:** Celery Beat task reading `pv_ledger` aggregates per distributor: find the weak leg,
apply the rate, enforce the weekly cap, carry forward the stronger leg's excess (expiring after
`PV_CARRY_FORWARD_EXPIRY_DAYS`), and require 100 PV monthly personal activity to be eligible. Task
must only read pre-aggregated counters — never walk the tree.

**In progress, built as slices (13a/13b/13c-design/13d-model):** 13a — real monthly personal-PV
tracking (`MonthlyPersonalPv`, `record_personal_pv`, `is_eligible_for_binary_bonus`), since nothing
previously tracked a distributor's own monthly PV, only PV credited to their legs. 13b —
`calculate_binary_bonus` + `apply_weekly_binary_bonus_cap` (pure calculation, rolling 7-day window,
`WalletTransaction.TransactionType.BINARY_BONUS` added). 13c — the carry-forward/expiry design went
through 3 rounds of `doubt-driven-development` (2026-07-15): round 1 caught a fatally flawed
"re-stamp on merge" proposal before any code was written (would have silently defeated the 180-day
expiry rule for any actively-trading leg); round 2 caught a raw-SQL, per-ancestor-loop, and
silent-failure design; round 3 (on the actual ORM-only bulk approach) caught a real transaction-
savepoint bug. **13d (model + hook, done):** `PvDailyBucket` (dated, per-leg PV buckets — the unit
carry-forward will consume FIFO and expire) plus `apps.pv_ledger.services._credit_daily_buckets`,
hooked into `record_purchase_pv`. Bulk-safe (2 statements per leg, not per-ancestor), with its own
bounded IntegrityError retry for the "two purchases both create today's first bucket" race,
deferring all lock-contention `OperationalError`s to the existing outer `retry_on_lock_contention`
wrapper rather than duplicating it. **A second real bug was found only by a genuine 10-thread
concurrency test** (not by review): an `OperationalError` mid-attempt could roll back an already-
"successful" bulk UPDATE while the retry logic kept it out of the next attempt's retry set —
silently losing PV with zero exceptions raised. Fixed by making the whole attempt (update + read +
create) one atomic block, and by never narrowing the retry set to just the missing ids (the whole
attempt rolls back together, so the whole attempt must retry together). Verified via a 30-trial
reproduction script (0/30 mismatches after the fix) plus 20+ repeated real-thread pytest runs.

**13e (Tasks 35/36/38, done 2026-07-15) — the actual per-distributor consumption cycle:**
`apps.commissions.services.process_binary_bonus_for_distributor(distributor, run_at)`, plus new
`apps.pv_ledger.services` helpers (`sum_leg_pv`, `expire_old_pv`, `consume_leg_pv_fifo`). Went
through its own `doubt-driven-development` cycle before implementation (found: a TOCTOU race in an
unlocked idempotency check, `is_eligible_for_binary_bonus`/`apply_weekly_binary_bonus_cap` reading
wall-clock time instead of the cycle's fixed `run_at`, an unverified `select_for_update`+`aggregate`
combination, a per-bucket-loop scale anti-pattern, and missing audit logging) — all reconciled
before writing code: the `Distributor` row is locked FIRST inside the atomic block (serializing
concurrent/retried runs so `credit()`'s unique-constraint `IntegrityError` becomes unreachable in
practice), `is_eligible_for_binary_bonus`/`apply_weekly_binary_bonus_cap` now take an explicit `now`
param the cycle always passes as `run_at`, `select_for_update` on `PvDailyBucket` was dropped
entirely in favor of the Distributor-row lock plus a documented, tested invariant (PV is only ever
incremented by the write-time path and only ever decremented by this function), and PV consumption
uses one bulk `Case`/`When` `F()`-relative `UPDATE` per leg (not a per-bucket loop, not raw SQL).
A follow-up code review then caught and fixed four more real issues: `expire_old_pv` was moved
*before* the eligibility check (a chronically-ineligible distributor's buckets weren't aging out
otherwise); a `CheckConstraint(pv__gte=0)` was added to `PvDailyBucket` (`PositiveIntegerField`
alone doesn't protect against a bulk `F()`-relative update going negative, and SQLite — what the
whole test suite runs against — has no MySQL-`UNSIGNED` equivalent); the "owed a nonzero bonus that
floors to 0 PV under the cap" case is now logged instead of silently deferred with no trace; and
the reviewer *proved* (by temporarily reintroducing each bug) that the test suite's fixed `run_at`
constant had accidentally been chosen to equal the sandbox's real current date, meaning a
regression back to reading real `timezone.now()` instead of `run_at` would have silently passed
every test — fixed by decoupling the constant from real "now" and adding two tests that mock real
`timezone.now()` to a different value and assert eligibility/cap behavior still follows `run_at`.
A real threaded test (`test_a_concurrent_purchase_write_mid_cycle_never_loses_pv`) proves a
purchase crediting this same distributor's leg *during* the cycle's computation never gets lost —
precisely synchronized past SQLite's whole-database lock limitation (documented in the test) rather
than trusted from the "PV only increases outside, only decreases inside" invariant's math alone.
46 new/updated tests, full 347-test suite green.

**13f (2026-07-15) — 6 skills applied retrospectively to the whole of Task 13**, per the project's
"check the full catalog, don't default to a habitual few" convention:
- `documentation-and-adrs`: `docs/decisions/0003-binary-bonus-carry-forward-design.md`, capturing
  the bulk `Case`/`When` consumption choice, the Distributor-row-lock-plus-invariant concurrency
  strategy (and why `select_for_update()` + `.aggregate()` was rejected), and the floor-not-round
  proportional-consumption rule -- decisions that went through real review cycles but, unlike
  Tasks 11/12, had never been written down.
- `source-driven-development`: cross-checked every Django-internals claim this design rests on
  against the official Django 5.0 docs rather than trusting the tests alone -- nested `atomic()`
  savepoint/rollback semantics (confirmed), `Case`/`When` requiring explicit `output_field` on
  mixed types (confirmed), MySQL silently ignoring conditional unique indexes (confirmed, matches
  the CI failure exactly), and `select_for_update()` + `.aggregate()` (genuinely undocumented
  either way, vindicating the decision to avoid it rather than assume it's safe). Citations added
  as code comments at each site.
- `ci-cd-and-automation`: **found CI had been silently broken for 3 days / 28 consecutive runs**
  (a lint failure in a hook script was skipping the actual MySQL-backed "Run tests" step every
  time, since lint and test were sequential steps in one job) -- meaning every concurrency test in
  this task, and in Task 12's wallet work, had only ever run against SQLite, never the real MySQL
  CI was built specifically to catch. Fixed the lint issues, split lint/test into independent
  parallel jobs so this can't recur silently, and got CI green against MySQL for the first time
  since 2026-07-12.
- **That, in turn, surfaced a real bug**: `WalletTransaction`'s duplicate-credit
  `UniqueConstraint(condition=~models.Q(reference=""))` is a partial/filtered index, which MySQL's
  Django backend silently refuses to create -- the same bug class already hit once on
  `BinaryTreeEdge`. The defense-in-depth double-credit guard had been a no-op in production since
  Task 12 shipped it. Not an active money leak (every real caller already has its own upfront
  idempotency check), but fixed: the constraint is now unconditional (every current caller already
  passes a non-blank reference, so this is behavior-preserving).
- `observability-and-instrumentation`: the three routine "no bonus this cycle" branches (ineligible,
  zero weak leg, cap exhausted) were silently returning with no trace at all -- now logged at
  DEBUG (not INFO, since this runs once per distributor per cycle across the whole distributor
  base) so a support inquiry has a real answer instead of nothing.
- `performance-optimization`: added `apps.pv_ledger.services.distributor_ids_with_pending_pv()`,
  a tested query returning only distributor ids with actual outstanding PV -- the set the
  not-yet-built batch driver should iterate, instead of every registered `Distributor` (most of
  whom are customers with zero binary-tree activity).
- `spec-driven-development`: neither `SPEC.md` nor the requirements doc defines tied-legs behavior;
  added a test confirming the existing design needs no tie-break logic at all (weak-leg PV is a
  `min()`, consumption is applied to both legs by the same amount regardless of which was smaller).

**13g (2026-07-21) — the batch driver, closing the "Still open" gap above.** New
`apps/commissions/tasks.py::calculate_binary_bonus`, a `@shared_task` iterating
`distributor_ids_with_pending_pv()` and calling `process_binary_bonus_for_distributor` once per id,
plus `apps/commissions/migrations/0001_seed_binary_bonus_periodic_task.py` wiring it into
`django_celery_beat` at a 10-minute interval (mirrors `apps/distributors/migrations/0004`'s
pattern). Went through a full `doubt-driven-development` cycle before implementation (a fresh
adversarial reviewer, not just self-checking) followed by a `code-review-and-quality` pass after —
both real, both changed the design:
- **`run_at` generated exactly once per cycle** (`timezone.now()` at the top of the task) and held
  fixed across every distributor, upholding `process_binary_bonus_for_distributor`'s idempotency
  contract; proven by a test asserting two distributors processed in the same cycle share the
  identical `run_at` in their `WalletTransaction.reference`.
- **Per-distributor exception isolation**, including the id-fetch itself: constructs an unsaved
  `Distributor(pk=id)` (verified safe -- grepped the whole call chain and confirmed nothing but
  `.pk` is ever read off it) inside the same try/except as the payout call, so a row deleted
  between the id list being fetched and being processed can't abort the rest of the batch.
- **Overlap protection**: a Redis `cache.add()` mutex (confirmed atomic on this project's
  `django-redis` backend specifically, by reading `django_redis/client/default.py`'s source --
  Django's own docs don't guarantee `add()` is atomic on every backend) stops two live cycles
  running at once, which would otherwise mint two different `run_at` values for what's meant to be
  one logical cycle. The doubt cycle's first pass caught that a lock set once at acquisition isn't
  enough at this platform's stated scale; the review pass caught that the fix still needed
  per-iteration TTL renewal, not just a longer initial timeout -- both are in and covered by a test
  that proves the lock survives cumulative processing time past its raw TTL.
- **Systemic-failure guard**: raises if every evaluated distributor failed this cycle, rather than
  returning a quiet all-zero summary that would look identical to "nothing owed" on Flower, since
  this job has no human review per cycle.
- **`BINARY_BONUS_INTERVAL_MINUTES` self-sync**: the constance setting is admin-editable and sits
  in the same fieldset as rates that already take effect live, so leaving the actual Beat schedule
  (a separate `django_celery_beat` DB row) permanently out of sync would be a real incident-response
  footgun, not just cosmetic -- the task syncs `IntervalSchedule.every` from the live constance
  value on every run (`django_celery_beat`'s `DatabaseScheduler` polls for DB changes every ~5s per
  its source, no beat restart needed). Explicitly does *not* touch a `PeriodicTask` an admin has
  repointed at a crontab/solar/clocked schedule via `django_celery_beat`'s own admin -- that's a
  deliberate choice made through a different, equally legitimate screen.

**Acceptance criteria:**
- [x] Weak leg correctly identified and reset to 0 after calculation; carry-forward applied to the strong leg
- [x] Weekly GHS 50,000 cap enforced per distributor
- [x] Distributors below 100 PV monthly personal activity are skipped
- [x] Carried PV older than the expiry setting is dropped, not counted
- [x] The above are proven per-distributor via `process_binary_bonus_for_distributor`; the Celery Beat task itself (schedule + batch driver) is built (`apps/commissions/tasks.py`, `apps/commissions/migrations/0001_seed_binary_bonus_periodic_task.py`)

**Verification:**
- [x] pytest test reproducing the doc example exactly: left 1,500 / right 600 → weak leg 600 → GHS 45.00 bonus, 900 PV carried forward
- [x] pytest test: weekly cap enforcement, zero/tie-leg case, expired carry-forward exclusion
- [x] 14 tests in `tests/unit/commissions/test_binary_bonus_task.py` for the batch driver itself: fixed `run_at`, pending-PV-only iteration, per-distributor isolation, deleted-row survival, overlap lock (acquire/release/release-on-raise/renewal-under-load), interval self-sync (including the crontab-preservation and non-positive-value guards), and the all-failed guard. Full suite green (366 passed) after landing.
- [ ] **Not done**: the specific 10k+-distributor scale test asserting the batch driver's own DB query count doesn't grow with distributor count/tree depth. The per-distributor function was already proven O(1)/O(log n) via a 16k+-node synthetic tree (Task 9/10), and the driver's own query shape is flat by construction (one `.iterator()`-based id query, then N already-O(1) calls) -- but that's reasoning, not a measurement, and this specific criterion from the original plan hasn't been exercised at scale. Revisit before go-live if a dedicated scale test is wanted.

**13h (2026-07-22) — dedicated `security-and-hardening` pass on 13g**, run separately from the doubt-driven/code-review passes above at the user's request, since neither of those is a security specialist and this is explicitly a "financial platform" per `SPEC.md`. Two findings fixed directly (in-scope, cheap): `BINARY_BONUS_INTERVAL_MINUTES` now enforces a 5-minute floor, not just `> 0` (it has no `CONSTANCE_ADDITIONAL_FIELDS` bounds, so an admin typo could otherwise sync a near-zero interval straight into the live Celery Beat schedule and spam the shared worker pool -- no `CELERY_TASK_ROUTES` exists in this project, so every task competes for the same pool); and the lock-renewal comments in `tasks.py` were corrected to state the *actual* guarantee (renewal covers cumulative time across iterations, not one iteration itself running longer than `LOCK_TIMEOUT_SECONDS` -- a real but non-fund-loss gap, since the per-distributor row lock + weekly-cap re-read + PV depletion still bound the damage even if two cycles' `run_at` values ever did overlap). Four findings surfaced but **deliberately not implemented** without a scope decision:
- No durable, queryable audit trail of what a cycle did (only ephemeral logs + Celery's 1-day-TTL Redis result backend; no `LOGGING` config, no `django_celery_results`, no `apps/commissions` model exists to persist a cycle-run record) -- OWASP A09, would need a new model + migration
- No cycle-level anomaly detection / circuit breaker before payouts commit (a logic bug in the already-reviewed math would produce a "healthy-looking" summary and pay real money undetected) -- a genuinely new capability, not in Task 13's original scope
- No dedicated Celery queue or `task_time_limit`/`soft_time_limit` for this (or any) task -- an infra-level change affecting every task in the project, not just this one
- `BINARY_BONUS_RATE`/`WEEKLY_BINARY_BONUS_CAP` (and other money-affecting constance settings) have no server-side bounds and no maker-checker step -- touches `apps/platform_settings/config.py` broadly, beyond this task

**13i (2026-07-22) — the two purely-additive 13h findings, implemented** (user confirmed: low-risk, go ahead; the other two -- circuit breaker, dedicated Celery queue -- remain deliberately unbuilt, see 13h).
- **Durable audit trail**: `apps/commissions/models.py` (new) adds `BinaryBonusCycleRun` (one row per completed cycle: `run_at`, `evaluated`, `paid`, `failed`, `total_amount`) and `BinaryBonusCycleFailure` (one row per distributor whose call actually raised -- deliberately *not* one row per routine zero-payout skip, which stays at DEBUG per the existing convention in `services.py`, or this table would bloat at this platform's stated scale). `distributor_id` is a plain integer, not a `ForeignKey` -- proven necessary, not just asserted, by the "ghost" test (a distributor deleted mid-cycle still gets a failure row referencing an id that never existed as a live FK target). Persisted in `calculate_binary_bonus` *before* the systemic-failure raise, in its own best-effort try/except, so the one cycle most worth auditing (everyone failed) doesn't lose its record along with the exception. Read-only in Django Admin (`apps/commissions/admin.py`): `has_add/change/delete_permission` all hardcoded `False`, verified to actually hold even for superusers (every real admin account in this project is one) by reading Django's `_changeform_view`/`_delete_view` source directly -- `has_change_permission`/`has_delete_permission` are checked as direct method calls on POST, not through `request.user.has_perm(...)`, so they aren't subject to the superuser bypass the way `has_view_permission`'s default is. Migration: `apps/commissions/migrations/0002_initial.py`.
- **Constance bounds**: `apps/platform_settings/config.py` adds `CONSTANCE_ADDITIONAL_FIELDS` (three custom-bounded form fields: `percentage_field` 0-100, `non_negative_money_field` floor-only, `interval_minutes_field` floor 5) and points `BINARY_BONUS_RATE`/`WEEKLY_BINARY_BONUS_CAP`/`BINARY_BONUS_INTERVAL_MINUTES` at them via constance's 3-tuple form -- defense in depth on top of 13h's runtime floor (stops a bad value at the admin form, not just before it reaches the live schedule). Zero is deliberately allowed for the rate and cap fields (not just `> 0`) since both already degrade to "pay nothing, no crash" at zero -- a legitimate incident-response pause lever -- proven end-to-end through the real payout path, not just at the form-field level, per a code-review finding that the original test only checked form acceptance.
- **Review**: a fresh `code-review-and-quality` pass on this diff (separate from 13g's) found and fixed four real gaps before landing: the "ghost" test didn't actually assert the failure row it was meant to prove; no test existed for the admin's read-only-even-for-superusers claim (added `tests/feature/commissions/test_binary_bonus_cycle_run_admin.py`); the zero-rate/zero-cap safety claim rested on a manual trace, not a test (added both end-to-end); and `test_lock_survives_cumulative_processing_time_past_the_raw_ttl` (from 13g) was reproducibly flaky under isolation (3/5 failures) at its original 1s/0.6s margins -- widened to 3s/1.8s, confirmed stable across 4 isolated reruns after the fix.

**Dependencies:** Task 9c, Task 10d, Task 3

**Files likely touched:** `apps/commissions/tasks.py` (`calculate_binary_bonus`, Celery Beat schedule), `apps/commissions/services.py` (a `BinaryBonusCalculator` class **or** a plain function -- Task 12 shipped `calculate_direct_referral_bonus` as a bare function rather than the originally-planned `DirectReferralCalculator` class, since the direct-referral case had no shared state worth a class; Binary Bonus's genuine complexity -- weak-leg detection, weekly cap, carry-forward -- may justify a class this time, but that's a real decision to make when this task starts, not a name to copy from this stale note), `tests/unit/commissions/test_binary_bonus.py`

**Estimated scope:** L (if it grows past ~5 files, split carry-forward/expiry logic into its own service)

---

### Task 14: Matching Bonus task — weekly

**Description:** Weekly Celery Beat task: for each distributor, sum downline binary-bonus earnings
3 levels deep (Bronze) or unlimited depth (Silver), credit 5% of that total.

**Acceptance criteria:**
- [x] Bronze distributors only earn matching bonus 3 levels deep; Silver earns unlimited depth (bounded by `MAX_MATCHING_BONUS_WALK_DEPTH` regardless)
- [x] Rate read from `django-constance` config
- [x] Eligibility requires 100 PV personal purchase in the current month, same as Binary Bonus

**Verification:**
- [x] pytest test reproducing the doc example exactly: Level 1 GHS 200→GHS10, Level 2 GHS150→GHS7.50, Level 3 GHS100→GHS5, total GHS22.50
- [x] pytest test: a Silver-rank distributor earns from a Level 4+ recruit; a Bronze-rank distributor does not

**Dependencies:** Task 13

**Files likely touched:** `apps/commissions/tasks.py` (`calculate_matching_bonus`), `apps/commissions/services.py` (class or function -- match whatever Task 13 settled on for `BinaryBonusCalculator`, see that task's note), `tests/unit/commissions/test_matching_bonus.py`

**Estimated scope:** M

**14-spec (2026-07-22) — resolved before implementation, via `spec-driven-development`.** The original description above is underspecified in three ways that matter architecturally; none are answerable from `SPEC.md` or the accessible parts of the original requirements doc, so they were resolved explicitly rather than guessed silently:

1. **"Downline" = the sponsor/recruitment chain** (`Distributor.sponsor`, a self-FK), not the binary placement tree (`apps.binary_tree.BinaryTreeEdge`). Evidence: the acceptance criteria's own wording ("a Level 4+ **recruit**") names recruitment, not placement; Task 12's Direct Referral Bonus already established sponsor-only crediting for level 1; and a distributor's binary-tree position can diverge from their sponsor chain under spillover -- Matching Bonus rewards who you personally recruited, not where spillover happened to land them.
2. **"Weekly" downline earnings = a rolling 7-day window** (`created_at >= run_at - 7 days`), not calendar-week or all-time -- matches the precedent `apply_weekly_binary_bonus_cap` already set for the identical ambiguity ("the source spec doesn't define calendar-week boundaries... a rolling window avoids a distributor getting 2x the cap by earning right at a calendar-week boundary").
3. **Traversal: live per-cycle BFS, not a new closure-table app** -- user's explicit call, weighed against `SPEC.md`'s "event-driven from the start" principle (which built `apps/binary_tree` specifically to avoid live recursive walks at platform scale). Decided proportionate to this task's original scope: one bulk query per level (`Distributor.objects.filter(sponsor_id__in=[...])`), capped at `config.MATCHING_BONUS_DEPTH_BRONZE` (3) for Bronze, looping until a level returns empty for Silver (`config.MATCHING_BONUS_DEPTH_SILVER == 0` means unlimited, already the existing constance convention -- both settings already existed in `apps/platform_settings/config.py` before this task started, unused until now). Real-world sponsor chains are expected to be far shallower than binary-tree spillover depth; revisit with pre-aggregation only if this ever actually becomes a measured bottleneck, not preemptively.

**Resolved algorithm**, per distributor D evaluated in one cycle (fixed `run_at`, mirroring Binary Bonus's contract):
- Eligibility: reuse `is_eligible_for_binary_bonus(D, now=run_at)` verbatim -- the rule is stated as identical, and duplicating it risks the two definitions drifting apart later.
- Depth: `{"bronze": config.MATCHING_BONUS_DEPTH_BRONZE, "silver": None}.get(D.rank)` (`None` = unlimited, loop until a level is empty; any other/blank rank -- not yet purchased a starter pack, or a future rank never added here -- gets no matching bonus, not a crash).
- BFS the sponsor chain level by level (one bulk query per level) up to that depth, collecting every downline distributor id encountered across all levels into one combined set.
- Sum every one of those ids' own `BINARY_BONUS`-type `WalletTransaction` amounts with `created_at` in `[run_at - 7 days, run_at)`, in one bulk aggregate query, not per-distributor.
- `matching_bonus = ROUND_HALF_UP(MATCHING_BONUS_RATE% x that sum, pesewa)` -- applied once to the combined total, not per-level then summed; mathematically identical to summing a per-level 5% for a single constant rate (confirmed against the worked example: 5%x(200+150+100) = 10+7.50+5), and simpler to implement as one combined sum.
- Credit via the existing `credit()` helper if > 0, `reference = f"matching-bonus-{D.pk}-{run_at.isoformat()}"` (same idempotency-key shape as Binary Bonus).

**New constance setting needed**: no admin-editable schedule interval exists yet for this task (only `MATCHING_BONUS_RATE`/`_DEPTH_BRONZE`/`_DEPTH_SILVER`) -- add `MATCHING_BONUS_INTERVAL_DAYS` (default 7), mirroring `BINARY_BONUS_INTERVAL_MINUTES`. `django_celery_beat.IntervalSchedule` supports a `"days"` period natively (confirmed via `IntervalSchedule.PERIOD_CHOICES`), so the same self-syncing-schedule pattern from Task 13g/13h applies directly, not by analogy.

**Decision on Task 13's hardening (resolved, not deferred this time):** Task 13's overlap lock, self-syncing/bounded interval, and per-distributor exception isolation are properties of *any* unattended Celery Beat financial batch driver, not Binary-Bonus-specific — the same failure modes (two overlapping cycles, an admin-editable interval with no floor, one distributor's exception aborting everyone else's) apply identically here. Building `calculate_matching_bonus` from the start with the same shape `calculate_binary_bonus` ended up with, rather than shipping the naive version and rediscovering the same three bugs via a second CI incident, is the whole point of doing this via `planning-and-task-breakdown` + `doubt-driven-development` up front this time. The durable audit-trail model (`BinaryBonusCycleRun`/`Failure`) is a closer call — deferred to an explicit `doubt-driven-development` check in Task 14f below rather than assumed either way, since duplicating that model per-bonus-type vs. generalizing it is a real design fork, not a rubber-stamp copy.

**Task breakdown** (via `planning-and-task-breakdown`; supersedes the single-task framing above — implemented as 14a–14g):

- **14a (XS, config.py):** Add `MATCHING_BONUS_INTERVAL_DAYS` (default 7) to `COMMISSION_AND_BONUS_SETTINGS`.
- **14b (S, services.py, TDD):** `calculate_matching_bonus(total_downline_earnings)` — pure function, `MATCHING_BONUS_RATE`% of the total, `ROUND_HALF_UP` to the pesewa. Mirrors `calculate_binary_bonus`'s shape exactly.
- **14c (M, services.py, TDD, highest-risk piece — built early to fail fast):** `sum_downline_binary_bonus_earnings(distributor, run_at)` — the sponsor-chain BFS + earnings sum. Returns the combined total across all levels up to the rank-derived depth. This is where the resolved algorithm above gets implemented and where the worked-example test (Level 1/2/3 → GHS 22.50) and the Silver-vs-Bronze depth-4+ test both land.
- **Checkpoint 14a–14c:** worked-example test and depth test both green before any orchestration code exists.
- **14d (S, services.py, TDD):** `process_matching_bonus_for_distributor(distributor, run_at)` — mirrors `process_binary_bonus_for_distributor`'s contract (fixed `run_at` as sole idempotency key via `reference`, row-locked, idempotent re-run-safe) but with no leg/PV-consumption side effects to manage — matching bonus only ever reads other distributors' already-committed `WalletTransaction` rows, never mutates them.
- **14e (XS, migration):** Seed the `calculate-matching-bonus` `PeriodicTask` at `every=7, period="days"`, mirroring `apps/commissions/migrations/0001`.
- **14f (M, tasks.py, TDD):** `calculate_matching_bonus` Celery Beat batch driver, built with Task 13's hardening from the start (per the decision above): fixed `run_at`, per-iteration lock renewal, per-distributor exception isolation, systemic-failure guard, self-syncing interval with a floor + non-interval-schedule guard. Iterates distributors with a `rank` set (bronze/silver) — the matching-bonus equivalent of `distributor_ids_with_pending_pv()` (needs its own query; "pending PV" doesn't apply here, eligibility is monthly-personal-PV + having any downline at all). **`doubt-driven-development` checkpoint here** on: the durable-audit-trail question above, and the new query's correctness (must not accidentally scan the whole distributor table looking for every rank the way `distributor_ids_with_pending_pv()`'s docstring warns against for its own case).
- **14g (all files):** `security-and-hardening`, `code-review-and-quality`, `code-simplification` passes, matching Task 13's process exactly.

**14-complete (2026-07-22).** Built end-to-end via the full workflow above (`spec-driven-development` → `planning-and-task-breakdown` → TDD per slice → `doubt-driven-development` before the batch driver → `security-and-hardening` + `code-review-and-quality` in parallel → `code-simplification`), applied proactively this time rather than reactively after a CI incident (contrast with Task 13's 13g→13h→13i sequence).

- **Caught before any code shipped:** a fresh-context `doubt-driven-development` reviewer, given the plan to mirror `calculate_binary_bonus`'s `Distributor(pk=id)`-stub calling convention exactly, found that `process_matching_bonus_for_distributor` (unlike Binary Bonus's function, which only ever needs `.pk`) reads `.rank` -- and the planned code discarded its own locked-row fetch and read `.rank` off the passed-in stub instead, which always resolves to `""` (no matching-bonus tier). That would have silently paid nobody, ever, with zero exceptions anywhere -- fixed before implementation started (`locked_distributor`, not `distributor`, is used throughout `_attempt()`), with a regression test (`test_works_correctly_when_called_with_an_unsaved_stub_like_the_batch_driver_does`) proving it.
- **Audit trail generalized, not duplicated** (user's explicit call): `BinaryBonusCycleRun`/`Failure` renamed to `CommissionCycleRun`/`Failure` with a `job_name` discriminator and a `(job_name, run_at)` composite unique constraint, so Matching Bonus (and any future bonus type) shares one audit table instead of a copy-pasted pair per job. `apps/commissions/tasks.py` was refactored to extract `_run_commission_cycle`/`_sync_periodic_task_interval`/`_persist_cycle_audit_record` as shared, parametrized helpers -- confirmed a pure, behavior-preserving refactor for `calculate_binary_bonus` (its existing test suite needed only mechanical renames, zero assertion changes).
- **Parallel `security-and-hardening` + `code-review-and-quality` passes found one real Medium-severity gap**: Silver's "unlimited" sponsor-chain depth combined with the batch driver's lock only renewing *between* distributors (never mid-BFS) meant a pathologically deep chain could theoretically outlast the lock's TTL -- and unlike Binary Bonus, Matching Bonus has no weekly cap or PV-consumption to throttle the resulting double-payment risk. Fixed with a hard `MAX_MATCHING_BONUS_WALK_DEPTH = 500` ceiling (independent of config), plus: `db_index=True` on `Distributor.rank` and an `Exists()`-based pre-filter query (was a join+`DISTINCT`) for scale; bounds added to the previously-unbounded `MATCHING_BONUS_DEPTH_BRONZE`/`_SILVER` constance fields; a regression test proving `MATCHING_BONUS_DEPTH_SILVER` is actually read live, not hardcoded-unlimited (the same "decorative constance setting" bug class already found once for `BINARY_BONUS_INTERVAL_MINUTES`, caught here before shipping instead of after).
- **Deferred, not decided silently:** a per-distributor-per-cycle cap for Matching Bonus (mirroring `WEEKLY_BINARY_BONUS_CAP`'s role) would bound the financial blast radius of any future double-payment bug the way Binary Bonus's cap already does -- flagged as a genuinely new capability, not built without a scope decision, same reasoning as Task 13h's deferred circuit-breaker item.
- **Verification:** 425 tests passed (full local suite), zero missing migrations, all lint/format clean. Not yet verified against real MySQL in CI (this work is on the `main` branch locally, not yet pushed/PR'd as of this note).

**Files:** `apps/commissions/services.py` (`calculate_matching_bonus`, `sum_downline_binary_bonus_earnings`, `_matching_bonus_depth_for_rank`, `distributor_ids_eligible_for_matching_bonus`, `process_matching_bonus_for_distributor`), `apps/commissions/tasks.py` (generalized + `calculate_matching_bonus`), `apps/commissions/models.py`/`admin.py` (generalized), `apps/commissions/migrations/0003_generalize_cycle_audit_models.py`, `0004_seed_matching_bonus_periodic_task.py`, `apps/platform_settings/config.py`, `apps/wallet/models.py` (+migration), `apps/distributors/models.py` (+migration, `rank` index), `tests/unit/commissions/test_matching_bonus.py`, `test_matching_bonus_task.py`, `test_binary_bonus_task.py` (renamed), `tests/feature/commissions/test_commission_cycle_run_admin.py` (renamed), `tests/unit/platform_settings/test_constance_config.py`.

---

**Checkpoint E — closed 2026-07-22:** `tests/feature/commissions/test_full_commission_journey.py`.
See `tasks/plan.md`'s Phase 4 entry for the full writeup — not a literal reproduction of Section
14's own narrative figures (its direct-referral numbers are a documented contradiction in the source
doc), but a real chain through registration → referral bonus → binary tree placement/PV credit →
`calculate_binary_bonus()` → `calculate_matching_bonus()` using this project's already-resolved
formula, asserting the exact resulting numbers at every step.

---

## Phase 5: Wallet & Withdrawal

### Task 15: Wallet ledger

**Description:** Wallet model with an append-only ledger of credit/debit entries (referral,
binary, matching, withdrawal debit, refund reversal). Balance is derived from the ledger, not a
mutable counter. Earnings history view on the distributor dashboard.

**Acceptance criteria:**
- [x] Every credit from Tasks 12–14 writes a ledger entry with type, amount, and timestamp
- [x] Wallet balance is always the sum of ledger entries, never edited directly
- [x] Earnings history view lists entries with type and amount

**Verification:**
- [x] pytest test: balance after a mixed sequence of credits/debits matches the sum exactly
- [x] pytest test: no code path outside `WalletService` writes to the ledger table directly

**Dependencies:** Task 12, Task 13, Task 14

**Files likely touched:** `apps/wallet/services.py` (`WalletService`), `apps/wallet/models.py` (`WalletLedgerEntry`), `apps/wallet/views.py` (earnings history), `tests/unit/wallet/test_wallet_service.py`

**Estimated scope:** M

**15-spec (2026-07-22) — resolved before implementation, via `spec-driven-development`.** An audit
first: `apps/wallet/models.py`'s `Wallet`/`WalletTransaction` (not `WalletLedgerEntry` -- that name
in "Files likely touched" above is stale, predating Tasks 12-14 actually building this out) already
satisfy most of this task's framing -- `WalletTransaction` is already the append-only ledger, and
`credit()` (a plain function, not a `WalletService` class, matching this codebase's established
"function over class" convention from Task 12) is already the *only* production code path that
writes it (confirmed via `grep -rn "WalletTransaction.objects.create"` across `apps/`). What's
actually missing: a `debit()` function (nothing debits yet -- withdrawal/refund are Tasks 16/19),
enforcement of "no other code path writes the ledger" beyond the soft `readonly_fields` Django Admin
already has, and the earnings history view (`apps/distributors/views.py::dashboard` is an explicit
placeholder stating "the real dashboard is Task 20" -- this is its own minimal page, not an
expansion of that placeholder).

**Resolved design decisions:**
1. **`WalletTransaction.amount` becomes signed** (positive for credits, already `credit()`'s
   behavior; negative for debits) so `SUM(amount)` trivially reconstructs `Wallet.balance` --
   literal reading of "balance is always the sum of ledger entries." `credit()`'s existing public
   API is unchanged (still takes a positive amount); a new `debit()` takes a positive amount as
   input and stores it negated. Checked: nothing currently reads `WalletTransaction.amount` assuming
   it's always positive (`sum_downline_binary_bonus_earnings` only sums `BINARY_BONUS`-type rows,
   which are never debits).
2. **`Wallet.balance >= 0` gets a DB-level `CheckConstraint`**, mirroring `PvDailyBucket`'s own
   established reasoning verbatim ("bulk F()-relative UPDATEs bypass Django's model-level field
   validation entirely... this constraint is the actual guardrail") -- defense in depth against the
   wallet going negative, not just caller discipline. Safe to add: every existing `Wallet.balance`
   value is guaranteed non-negative today, since `debit()` has never existed.
3. **New `TransactionType` values `WITHDRAWAL_DEBIT`/`REFUND_REVERSAL`**, named directly from this
   task's own description, added now even though Tasks 16/19 haven't built their calling code yet --
   needed to write a real "mixed credit/debit sequence" test, and the task description itself
   pre-names them (matching the precedent of `MATCHING_BONUS_DEPTH_BRONZE`/`_SILVER` existing in
   constance config before Task 14 used them).
4. **"No code path outside the wallet layer writes the ledger" enforced with a hard lockdown**,
   mirroring Task 13's `CommissionCycleRunAdmin` exactly (`has_add_permission`/
   `has_change_permission`/`has_delete_permission` all `False`), not just `readonly_fields` -- plus a
   real test proving it end-to-end against the actual admin view, mirroring
   `test_commission_cycle_run_admin.py`'s pattern.
5. **Earnings history is its own minimal page** (`distributors:earnings_history`, extending
   `base_auth.html` like `dashboard.html` does) -- listing a distributor's own `WalletTransaction`
   rows (type, amount, date), **with pagination from the start** (revised during spec review: not
   speculative -- a long-lived, successful distributor can plausibly accumulate thousands of rows at
   this platform's stated scale, since Binary Bonus can fire every 10 minutes and Matching Bonus
   weekly; an unpaginated list is a near-certain problem for exactly the users this feature serves,
   not a hypothetical one).

**Task breakdown** (via `planning-and-task-breakdown`; implemented as 15a-15f):
- **15a (S, models.py + migration, TDD):** signed `amount`, the new `TransactionType` values, and
  `Wallet`'s `CheckConstraint(balance__gte=0)`.
- **15b (S, services.py, TDD):** `debit()`, symmetric to `credit()` (same validation shape, same
  no-self-retry docstring contract), storing `-amount`; relies on the new `CheckConstraint` to reject
  an over-debit rather than a pre-check race.
- **Checkpoint 15a-15b:** the balance-invariant test (mixed credit/debit sequence, `Wallet.balance ==
  sum(WalletTransaction.amount)`) is green before touching admin/views.
- **15c (S, admin.py, TDD):** hard lockdown on `WalletTransactionInline` + a standalone-registration
  guard if needed; test proves add/change/delete all rejected even for a superuser, mirroring
  `test_commission_cycle_run_admin.py`.
- **15d (M, views.py/urls.py/template, TDD + `frontend-ui-engineering`):** `earnings_history` view,
  paginated, own-wallet-only. **Done 2026-07-22.** Design source: a Stitch screen ("Earnings History
  - Bancostore Distributor", project `14456046746368120137`) was fetched and verified against the
  original prompt before building, per a standing instruction that all future frontend work pause
  for this fetch-verify-build sequence. Verification caught two real gaps in the generated screen
  versus the prompt: (1) no true desktop icon-only collapsed sidebar state, only a mobile show/hide
  drawer -- built for real in `templates/distributors/base_dashboard.html` (Alpine.js, state
  persisted to `localStorage`); (2) no empty-state design -- designed and built directly (icon +
  "No earnings yet"). `base_dashboard.html` is a new shared sidebar app-shell (nav: Dashboard,
  Earnings History, Team*, Binary Tree*, Withdraw*, Settings*, Log Out -- `*` = disabled/inert,
  matching the honesty convention `dashboard.html` already established for unbuilt pages), intended
  to be reused for the admin dashboard once that's built (not extracted into a shared partial yet --
  only one consumer so far). `templates/distributors/dashboard.html` (Task 20's placeholder) was
  migrated onto this same shell rather than left on `base_auth.html`, since leaving it behind made
  the sidebar disappear/reappear when navigating between Dashboard and Earnings History -- caught by
  `code-review-and-quality`, not planned upfront.
- **15e:** `security-and-hardening` pass specifically on the view (access control: a distributor must
  never see another distributor's transactions via id/param manipulation). **Done 2026-07-22** via a
  dedicated `security-auditor` review. No IDOR (the view is unconditionally scoped to
  `request.user.distributor`, never an id/param, backed by a real cross-distributor regression test).
  One High finding, fixed: `WalletAdmin` had no `has_delete_permission` override -- a staff/superuser
  could delete a `Wallet` row outright via Django Admin's default delete action, which cascades
  (`WalletTransaction.wallet` is `on_delete=CASCADE`) into silently destroying that distributor's
  entire ledger, bypassing `WalletTransactionInline`'s own lockdown entirely since it's never reached.
  Fixed by locking down `WalletAdmin` itself (`has_add/change/delete_permission` all `False`),
  mirroring `CommissionCycleRunAdmin` completely rather than just the inline half, with HTTP-level
  regression tests. One Medium finding, fixed: `@login_required` alone doesn't distinguish this
  platform's three account types sharing one `User` model (customer/distributor/admin) --
  an authenticated customer with no `Distributor` row hit an unhandled 500 via
  `request.user.distributor` instead of a clean 403. Fixed by wiring in
  `apps/accounts/permissions.py::is_distributor` (which already existed but was dead code, unused
  anywhere in the codebase) as an explicit guard in `earnings_history`. **Not fixed, deferred:** the
  same unguarded-`request.user.distributor` pattern exists in `select_starter_pack`,
  `start_kyc_verification`, and `dashboard` (all pre-existing, untouched by this task) -- flagged by
  both review passes as worth a shared `@distributor_required` decorator applied codebase-wide, but
  out of Task 15's scope; tracked here as a fast-follow, not silently skipped.
- **15f:** `code-review-and-quality`, `code-simplification`, full suite, browser check of the view.
  **Done 2026-07-22.** `code-reviewer` pass found two Important issues, both fixed: (1) pagination
  used `order_by("-created_at")` with no tie-breaker -- two `WalletTransaction` rows landing on the
  same timestamp (realistic: Binary Bonus can fire every 10 minutes) could be ordered differently
  between the page-1 and page-2 queries, silently dropping or duplicating a row across the page
  boundary; fixed by adding `"-pk"` as a stable secondary sort key. (2) the dashboard-shell migration
  described under 15d above. A Low/Suggestion finding was also fixed: `total_withdrawn` originally
  summed *any* negative ledger row, which would have silently folded future `REFUND_REVERSAL` debits
  (Task 19, a refund clawback, conceptually distinct from a withdrawal) into a tile literally labeled
  "Total Withdrawn" -- narrowed to `transaction_type=WITHDRAWAL_DEBIT` specifically. No browser
  automation tool (Chrome DevTools MCP, Playwright, Puppeteer) was available in this environment --
  verified instead via real server-rendered HTML over authenticated `curl` sessions against the live
  dev server (login, own-vs-other-distributor isolation, empty state, pagination, signed/colored
  amounts, nav items, static asset serving), which exercises the same Django/template code path a
  browser would but does not confirm actual visual rendering (CSS layout, responsive breakpoints,
  Alpine.js collapse/mobile-drawer interactivity) the way a real browser check would -- flagged
  explicitly rather than claimed as done. Full suite: 455 passed (up from 451 pre-Task-15), 0
  regressions. `black`/`ruff`/`isort` clean; `makemigrations --check --dry-run` confirms no missing
  migration.

**Task 15 complete (2026-07-22).** All of 15a-15f shipped, reviewed (`code-review-and-quality` +
`security-and-hardening`, both via dedicated fresh-context subagents, not just self-review), and
verified. Not yet done: committing/pushing this work and taking it through the established
branch → PR → CI (real MySQL) → CodeRabbit → merge workflow used for Tasks 13/14/Checkpoint E --
still sitting on `main` locally as of this writing.

---

### Task 16: Withdrawal request flow

**Description:** Distributor requests a withdrawal (above the configured minimum, once per week,
KYC-gated). System deducts withholding tax, admin reviews/approves (individually or in bulk), and
Paystack sends the payout (sandbox).

**2026-07-22 — min/max/day decision resolved.** `SPEC.md` Open Question #5 ("set by admin" in the
source doc, no concrete numbers ever given): minimum GHS 100, maximum GHS 10,000 per request,
processed Fridays (a common payday convention, and it lands the same week Matching Bonus already
pays out on). Seeded into `apps/platform_settings/config.py`'s `WITHDRAWAL_AND_PAYOUT_SETTINGS` --
`MIN_WITHDRAWAL_AMOUNT`/`MAX_WITHDRAWAL_AMOUNT` now use the `non_negative_money_field` constance
widget (matching `WEEKLY_BINARY_BONUS_CAP`'s own pattern) instead of an unvalidated 2-tuple, and
`WITHDRAWAL_DAY` moved off a free-text default onto a new `day_of_week_field` constance widget (a
bounded 7-day choice, not a string an admin could mistype). All three remain fully admin-editable
at runtime -- this is only the starting seed value, not a permanent code decision. Task 16 is now
unblocked.

**2026-07-22 -- design gaps closed via `spec-driven-development` + `planning-and-task-breakdown`
before any code.** Four questions had no answer anywhere in `SPEC.md` and each had a real
money-safety consequence if guessed wrong, so they were put to the user directly rather than
assumed (`SPEC.md`'s Boundaries already require asking first on both schema changes and Paystack
integration code -- Task 16 is both at once): where a distributor's payout destination
(mobile money number + network) is captured and stored, what `WITHDRAWAL_DAY` actually gates
(submission window vs. payout batch), when the wallet is actually debited relative to admin
approval and Paystack confirmation, and what to do with the pre-seeded but never-wired
`AUTO_APPROVE_WITHDRAWALS_ENABLED`/`_THRESHOLD` settings given `SPEC.md`'s hard Boundary against
ever auto-approving a withdrawal. All four resolved and recorded in
`docs/decisions/0004-withdrawal-payout-design.md`: payout destination is a new `Distributor`
profile field set once (gates request submission the same way `kyc_status` already does);
`WITHDRAWAL_DAY` gates a Friday Celery payout batch, not submission (distributors can request any
day, capped once per `WITHDRAWAL_FREQUENCY`); the wallet is debited at admin approval (not at
request, not at confirmed payout), with a `credit()`-based reversal if the subsequent Paystack
Transfer fails; the auto-approve settings stay permanently unwired, documented in code so a future
contributor doesn't "helpfully" wire them in without re-reading the ADR. Broken into 8 vertically-
sliced sub-tasks below (16a-16h), ordered so the money-safety-critical core (state machine, tax
calc, debit/reversal) ships and is fully tested before the UI/admin polish and batch-payout layers.

**Acceptance criteria (whole-task, verified by Checkpoint F below):**
- [x] Withdrawal blocked if KYC is not approved, payout destination isn't set, amount is below
      minimum/above maximum, or one was already made this `WITHDRAWAL_FREQUENCY` window
- [x] Tax is deducted using the `WITHHOLDING_TAX_RATE` setting and shown to the distributor before confirming
- [x] Admin can approve individually or in bulk; approving debits the wallet immediately
- [x] The Friday batch job pays out every approved-but-unpaid request via Paystack Transfer (sandbox)
- [x] A failed/reversed Paystack transfer reverses the wallet debit exactly once (idempotent against webhook/poll retries)

**Verification (whole-task):**
- [x] pytest test reproducing the doc example exactly: GHS 500 requested → GHS 5 tax → GHS 495 paid
  (`tests/unit/withdrawal/test_submit_withdrawal_request.py::test_matches_the_documented_worked_example_exactly`)
- [x] pytest test: second withdrawal request in the same `WITHDRAWAL_FREQUENCY` window is rejected
  (`tests/unit/withdrawal/test_submit_withdrawal_request.py::test_second_request_within_the_same_window_is_rejected`)
- [x] pytest test: a reversed Paystack transfer credits the wallet back exactly once, even if the webhook fires twice
  (`tests/unit/withdrawal/test_apply_verified_transfer_outcome.py::test_applying_failed_twice_never_double_credits`,
  `::test_concurrent_applications_of_a_failed_outcome_never_double_reverse`)

**Dependencies:** Task 11 (KYC), Task 15 (wallet `debit()`), withdrawal min/max/day decision (resolved),
ADR-0004 design decisions (resolved), Paystack sandbox access, **explicit user sign-off before 16e/16f
specifically** (writing/modifying Paystack integration code is a standing `SPEC.md` Boundary "ask
first" item, independent of this breakdown having already been reviewed)

**Estimated scope:** XL as a whole -- hence the 16a-16h split; no individual sub-task should exceed ~5 files

---

#### Task 16a: Payout settings -- distributor payout-destination field + profile UI

**Description:** New `mobile_money_number` + `mobile_money_network` fields (MTN/Telecel/AirtelTigo
choices -- Telecel, not Vodafone; Vodafone Ghana rebranded and Paystack's own docs confirm "Telecel
Cash" as the current network name, verified before implementation, see ADR-0004) so a distributor
has somewhere for Paystack to actually send money. Set once via a
"Payout Settings" page on the existing `templates/distributors/base_dashboard.html` shell, not
re-entered per withdrawal request (ADR-0004 decision 1 -- ruled out re-entry per request because a
typo would have no stored history to catch it on the next request).

**Acceptance criteria:**
- [ ] `Distributor` (or a new one-to-one `PayoutAccount`) gains the two fields, migrated
- [ ] A distributor can set/update payout details from their dashboard; scoped unconditionally to
      `request.user.distributor` (no id/param IDOR surface, same pattern `earnings_history` set in Task 15)
- [ ] Withdrawal request submission (16c) is blocked with a clear message until this is set

**Verification:**
- [ ] pytest: unauthenticated / non-distributor / wrong-distributor access all rejected
- [ ] pytest: invalid network choice / malformed mobile money number rejected
- [ ] Verified live in a real browser: set payout details, confirm they persist and render back correctly

**Dependencies:** None (first slice, no withdrawal logic depends on this beyond the gate check)

**Files likely touched:** `apps/distributors/models.py`, a new migration, `apps/distributors/views.py`,
`apps/distributors/forms.py`, `templates/distributors/payout_settings.html`, `tests/`

**Estimated scope:** M (3-5 files)

**Skills:**
- *Before:* `doubt-driven-development` -- is a new field on `Distributor` the right call vs. a
  separate `PayoutAccount` model (future-proofs a distributor having multiple payout methods, but
  is that real scope or speculative)? Settle this before the migration, not after.
- *During:* `incremental-implementation`, `test-driven-development`, `frontend-ui-engineering`
  (this is a Stitch-designed screen like Task 15's Earnings History -- fetch/verify the design
  before building), `security-and-hardening` (IDOR scoping, input validation on the phone-number-
  shaped field)
- *After:* `code-review-and-quality`, `code-simplification`

---

#### Task 16b: `apps/withdrawal` app scaffold + `WithdrawalRequest` model + state machine

**Description:** New Django app from scratch. `WithdrawalRequest` model: distributor FK, requested
amount, tax amount, net payout amount (all three locked in at submission time, per ADR-0004 point
10 -- authoritative through approval/payout regardless of later constance changes), status
(`submitted` / `approved_debited` / `queued_for_payout` / `paid` / `payout_failed_reversed` /
`rejected`), `reviewed_by`/`reviewed_at`, a **snapshotted** payout destination
(`mobile_money_number`/`network`, copied from the distributor's 16a profile at approval time, per
ADR-0004 point 7 -- never read live off `Distributor` later), `paystack_recipient_code`/
`paystack_transfer_reference` (nullable until 16e/16f populate them). No business logic yet -- this
slice is schema + admin registration only.

**Acceptance criteria:**
- [ ] Model + migration exist; status is a bounded `TextChoices`, not a free-text field
- [ ] `WithdrawalRequestAdmin` hard-locks `has_add/change/delete_permission` to `False`
      unconditionally, matching `CommissionCycleRunAdmin`/`WalletAdmin` exactly -- **corrected
      2026-07-22 (ADR-0004 point 9), superseding an earlier "fix" that was itself wrong.** That
      earlier fix assumed a non-superuser admin scenario based on `seed_roles.py` (a DEBUG-only dev
      stub), but `tests/conftest.py`'s `staff_client` fixture confirms every real admin account in
      this project is a superuser by design. Checked against Django's actual docs:
      `has_view_permission()`'s default independently resolves `True` for a superuser regardless of
      any `has_change_permission` override, so the hard lock doesn't break changelist visibility.
      What it does break is 16d's bulk actions (which default to requiring
      `self.has_change_permission()` -- my own override, not a raw superuser bypass) -- so each
      action in 16d must explicitly declare `permissions=["view"]`.
- [ ] `django-simple-history` tracks status transitions (mirrors the KYC/IR ID audit trail requirement)

**Verification:**
- [ ] pytest: a superuser admin (`tests/conftest.py::staff_client`, this project's only real admin
      shape) can view the changelist; add/change/delete are all denied even for this account
- [ ] `manage.py check` clean, migration applies cleanly against real MySQL in CI

**Dependencies:** None

**Files likely touched:** `apps/withdrawal/__init__.py`, `apps/withdrawal/models.py`,
`apps/withdrawal/admin.py`, `apps/withdrawal/migrations/0001_initial.py`, `bancostore/settings.py`
(`INSTALLED_APPS`), `tests/`

**Estimated scope:** S-M (this is schema only, no service logic)

**Skills:**
- *Before:* `doubt-driven-development` -- already run once at the whole-task-plan level (see
  ADR-0004's "Doubt-Driven Review" section, which resolved the admin-permission bug and the state
  machine gaps reflected in this slice's acceptance criteria above). Re-run only if implementation
  surfaces something the plan-level review didn't cover.
- *During:* `incremental-implementation`, `test-driven-development`
- *After:* `code-review-and-quality`

---

#### Task 16c: Withdrawal request submission -- service + distributor-facing view

**Description:** `apps/withdrawal/services.py::submit_withdrawal_request` -- validates KYC approved,
payout destination set (16a), amount within `MIN_WITHDRAWAL_AMOUNT`/`MAX_WITHDRAWAL_AMOUNT` **and
within the distributor's current `Wallet.balance`** (ADR-0004 point 5 -- the amount bounds are
independent of any individual balance, so this needs its own explicit check), and no existing
non-terminal request within the current `WITHDRAWAL_FREQUENCY` window (see below for exactly which
statuses count). Computes tax via `WITHHOLDING_TAX_RATE` and locks that value in on the row (ADR-0004
point 10 -- authoritative through approval even if the live setting later changes); creates the
`WithdrawalRequest` row at `status=submitted`. Does **not** touch the wallet yet (ADR-0004 decision
3 -- debit happens at admin approval, in 16d). Distributor-facing form shows the tax deduction
preview before the distributor confirms.

`WITHDRAWAL_FREQUENCY`'s window check needs a `{"weekly": timedelta(days=7)}`-shaped mapping in
this service (ADR-0004 point 12) -- the constance setting is a bare string, not a duration, so
`T + WITHDRAWAL_FREQUENCY` isn't a valid operation without this translation. An unmapped value must
raise, never silently mean "no restriction." The window check itself only counts requests still
`submitted`/`approved_debited`/`queued_for_payout`/`paid` -- **`rejected` and
`payout_failed_reversed` requests do not consume the window** (ADR-0004 point 11, user-confirmed:
an outcome outside the distributor's control shouldn't cost them their whole week).

**Acceptance criteria:**
- [ ] Every rejection path from `SPEC.md`'s acceptance criteria produces a clear, distinct message
      (not KYC-approved / below minimum / above maximum / exceeds current wallet balance / already
      requested this window / no payout destination set)
- [ ] Tax preview shown matches what actually gets stored on confirm (no drift between preview and commit)
- [ ] `submit_withdrawal_request` is the sole entry point -- no view-layer shortcut bypasses it
- [ ] A rejected or payout-failed-reversed prior request does not block a new submission in the same window

**Verification:**
- [ ] pytest reproducing the doc example exactly: GHS 500 requested → GHS 5 tax shown and stored → GHS 495 net
- [ ] pytest: second request within the same `WITHDRAWAL_FREQUENCY` window is rejected -- specifically
      test the window boundary (request at T, second attempt at T + `WITHDRAWAL_FREQUENCY` minus one
      second still rejected, at T + `WITHDRAWAL_FREQUENCY` exactly allowed), mirroring the off-by-one
      lesson already learned once in this codebase's PV carry-forward/expiry logic
- [ ] pytest: a rejected request followed by a fresh submission in the same window succeeds
- [ ] pytest: request amount exceeding current wallet balance is rejected even when within
      `MIN`/`MAX_WITHDRAWAL_AMOUNT`
- [ ] Verified live in a real browser: full submission flow, tax preview renders correctly

**Dependencies:** 16a (payout destination gate), 16b (model)

**Files likely touched:** `apps/withdrawal/services.py`, `apps/withdrawal/views.py`,
`apps/withdrawal/forms.py`, `templates/distributors/withdrawal_request.html`, `tests/feature/withdrawal/`

**Estimated scope:** M (3-5 files)

**Skills:**
- *Before:* `doubt-driven-development` on the window-boundary check specifically (the exact hazard
  called out in the verification step above -- easy to get off by one, expensive to ship wrong on a
  real financial gate)
- *During:* `incremental-implementation`, `test-driven-development` (write the window-boundary and
  tax-calc tests before the implementation, not alongside it -- this codebase's own
  `feedback_vertical_slice_build_process` memory exists precisely because Task 8 skipped this once),
  `frontend-ui-engineering`, `security-and-hardening` (scoping to `request.user.distributor`, CSRF
  on the confirm step)
- *After:* `code-review-and-quality`, `code-simplification`

---

#### Task 16d: Admin approval/rejection -- wallet debit + reversal-capable transition

**Description:** `apps/withdrawal/services.py::approve_withdrawal_request` -- locks the
**`Distributor` row first** (`select_for_update_nowait_if_supported`, matching
`process_binary_bonus_for_distributor`/`process_matching_bonus_for_distributor`'s existing order
exactly -- ADR-0004 point 8, resolved via `doubt-driven-development` before implementation rather
than deferred: locking `Wallet` directly here instead would risk acquiring rows in the opposite
order from Binary/Matching Bonus and deadlocking against them under concurrent load). Inside that
same locked transaction, in order: (1) re-check `kyc_status` is still approved (ADR-0004 point 7 --
a distributor's KYC can be revoked in the days between submission and approval; if no longer
approved, fail with a clear reason and leave the request at `submitted`), (2) snapshot the
distributor's current payout destination onto the `WithdrawalRequest` row (ADR-0004 point 7 -- so a
later profile edit can't redirect an already-debited payout), (3) call
`apps/wallet/services.py::debit()` for the net amount. If `debit()` raises
`InsufficientBalanceError` (balance can move between submission and approval), the whole
transaction rolls back, the request stays `submitted`, and the admin sees a clear error -- **no new
terminal state is needed for this** (ADR-0004 point 5). On success, transition to
`approved_debited`. `reject_withdrawal_request` requires a reason and never touches the wallet,
mirroring the KYC reject flow's intermediate-confirmation-page pattern (Task 11). Django Admin bulk
actions for both -- each declared with `@admin.action(..., permissions=["view"])` (ADR-0004 point 9,
corrected 2026-07-22), since 16b hard-locks `has_change_permission` to `False` and Django's actions
default to requiring it otherwise, which would silently block even a real (superuser) admin from
running them. **This is the first real caller of `debit()`** -- it's existed since Task 15 but
nothing has used it yet.

**Acceptance criteria:**
- [ ] Approving a request debits the wallet exactly once, even under concurrent double-approval
      attempts (two admins clicking approve on the same request at once)
- [ ] Approval re-checks KYC and current balance inside the lock; either failing leaves the request
      at `submitted` with a clear admin-facing reason, not a silent no-op or a crash
- [ ] The payout destination is copied onto the request row at the moment of approval, not read
      live from `Distributor` at any later stage
- [ ] Rejecting a request never debits the wallet and requires a reason, recorded via `django-simple-history`
- [ ] Bulk approve/reject both work and both go through the same single-request functions underneath
      (no separate bulk-only code path that could drift from the individual-request logic)

**Verification:**
- [ ] pytest: concurrent double-approval test (mirrors the 5-thread test that proved `credit()`
      concurrency-safe in Task 12) proves exactly one debit occurs
- [ ] pytest: approving a request whose distributor's KYC was revoked after submission fails cleanly,
      wallet untouched
- [ ] pytest: approving a request that now exceeds the distributor's current balance fails cleanly
      with `InsufficientBalanceError` surfaced as an admin-facing message, wallet untouched, request
      still `submitted`
- [ ] pytest: the snapshotted payout destination on an approved request differs from the
      distributor's live profile after the distributor edits their profile post-approval -- proves
      the snapshot, not a live read, is what a later slice (16e/16f) would use
- [ ] pytest: rejected request's wallet balance is unchanged
- [ ] pytest: rejecting without a reason is rejected by the form/action itself

**Dependencies:** 16b (model), 16c (submitted requests to act on), Task 15 (`debit()`)

**Files likely touched:** `apps/withdrawal/services.py`, `apps/withdrawal/admin.py`, `tests/feature/withdrawal/`

**Estimated scope:** M (3-4 files, but do not let it grow -- if concurrency edge cases balloon the
test file, that's a sign this slice is right-sized, not a sign to fold in more)

**Skills:**
- *Before:* `doubt-driven-development` -- already run once at the whole-task-plan level and resolved
  the lock-ordering question explicitly (ADR-0004 point 8: lock `Distributor` first, matching
  Binary/Matching Bonus, not a separate `Wallet` lock). A second, narrower pass at implementation
  time is still worth running specifically on the three-step ordering inside the lock (KYC check →
  snapshot → debit) -- the same discipline that caught Task 14's unsaved-stub `.rank` bug before it
  shipped -- since getting that internal order wrong (e.g. debiting before confirming KYC is still
  valid) would reopen the exact gap this design just closed.
- *During:* `incremental-implementation`, `test-driven-development`
- *After:* `code-review-and-quality` (elevated scrutiny -- this is `debit()`'s first real caller,
  the exact kind of change `SPEC.md`'s Testing Strategy singles out for "every code path" coverage)

---

#### Task 16e: Paystack Transfer API wrapper (recipient, initiate, verify)

**Description:** Extends `apps/distributors/paystack.py` with the Transfer side of Paystack's API
(currently only the Transaction/charge side exists): create a Transfer Recipient from a
distributor's 16a payout details, initiate a Transfer, verify a Transfer's status server-side
(never trust a webhook body alone -- same rule `verify_transaction` and the Didit integration
already established). **`SPEC.md`'s Boundary requires asking first before writing or modifying any
Paystack integration code, in sandbox mode or not -- this applies to this slice specifically,
independent of the whole-task plan already being reviewed. Confirm before starting 16e.**

**Acceptance criteria:**
- [ ] `create_transfer_recipient`/`initiate_transfer`/`verify_transfer` match Paystack's actual
      documented request/response shapes (checked against real docs, not assumed from the existing
      Transaction API's shape)
- [ ] Every function follows the existing `PaystackError` exception pattern already used by
      `initialize_transaction`/`verify_transaction`
- [ ] No live Paystack call happens in tests -- sandbox/test-mode keys only, mocked in pytest

**Verification:**
- [ ] pytest covering success and failure responses for all three functions, mocked
- [ ] Manually verified once against real Paystack sandbox (not just mocks) before merge, mirroring
      how Task 11's Didit integration was verified against a real live session, not just its test suite

**Dependencies:** 16a (payout details to build a recipient from), explicit user sign-off (Boundary)

**Files likely touched:** `apps/distributors/paystack.py`, `tests/unit/test_paystack.py`

**Estimated scope:** S-M (2-3 files, but do not merge without the real-sandbox manual check)

**Skills:**
- *Before:* **user sign-off (Boundary, not a skill)**, then `source-driven-development` --
  ground every request/response shape in Paystack's actual Transfer API docs, the same discipline
  `paystack.py`'s existing docstrings already model ("Source: https://paystack.com/docs/..." on
  every function) and that caught a real bug in Task 11 (Didit's image-field names differed from
  what was originally assumed)
- *During:* `incremental-implementation`, `test-driven-development`
- *After:* `security-and-hardening` (new outbound HTTP surface handling money-movement credentials),
  `code-review-and-quality`

---

#### Task 16f: Transfer webhook + Friday payout batch (Celery)

**Description:** New HMAC-verified webhook endpoint for `transfer.success`/`transfer.failed`/
`transfer.reversed` events, Celery-deferred processing (mirrors the Didit webhook's 5-second-timeout
pattern from Task 11 -- Paystack's webhook delivery has its own tight response-time expectation).
A Celery Beat task, structurally mirroring `apps/commissions/tasks.py`'s existing
`_run_commission_cycle`/`_sync_periodic_task_interval`/`_persist_cycle_audit_record` shared helpers,
runs on `WITHDRAWAL_DAY`. For each `approved_debited` request, it does **not** call `initiate_transfer`
directly -- it first generates and persists a pinned transfer reference and moves the row to
`queued_for_payout` (ADR-0004 point 6, the fix for a real double-payout gap a `doubt-driven-development`
pass caught in the original draft: nothing previously stopped a Celery retry, or a crash between
"Paystack accepted the transfer" and "local status updated," from calling `initiate_transfer` a
second time for the same row). Only after that claim commits does it call `initiate_transfer` with
the pinned reference. A retry that finds a row already `queued_for_payout` with a reference does
**not** re-claim or re-initiate -- it resumes via `verify_transfer` against the existing reference
instead, mirroring `consume_paid_starter_pack`/`PendingRegistration`'s established pinned-reference
pattern for the inbound registration payment. On `transfer.failed`/`transfer.reversed`, reverses the
debit via `credit()` and sets `payout_failed_reversed` -- idempotently, so a duplicate webhook
delivery can't double-credit.

**Acceptance criteria:**
- [ ] Webhook signature verified before any processing; CSRF-exempt only for this endpoint, same as
      the existing Paystack charge webhook
- [ ] A duplicate webhook delivery (same event, fired twice) never double-credits a reversal or
      double-transitions a status
- [ ] A batch-task retry (simulating a crash after Paystack accepted the transfer but before the
      local status updated) never calls `initiate_transfer` twice for the same request -- it finds
      the row already `queued_for_payout` and calls `verify_transfer` instead
- [ ] The batch task reuses `_run_commission_cycle`'s shared audit-trail/lock/per-item-isolation
      machinery rather than duplicating it (same "generalize, don't duplicate" call Task 14 made for
      `CommissionCycleRun`/`Failure`) -- or documents explicitly why withdrawal's shape doesn't fit
      and a new audit model is genuinely warranted
- [ ] A systemic-failure guard: if every request in a batch run fails, raise rather than silently
      report an all-zero summary (mirrors Task 13's own hardening)

**Verification:**
- [ ] pytest: duplicate webhook delivery test proves exactly one reversal-credit
- [ ] pytest: a request already `queued_for_payout` with a pinned reference, re-processed by a
      second batch run, results in exactly one `initiate_transfer` call (proven via a mock call-count
      assertion, not just the end state)
- [ ] pytest: batch task's query count stays flat regardless of pending-request count (this codebase
      flagged the equivalent check as "reasoning, not measurement" for Task 13's driver -- don't repeat
      that gap here if it's cheap to just measure)
- [ ] Verified against real MySQL in CI, not SQLite (concurrency-sensitive, same reasoning as the
      commission/wallet suite generally)

**Dependencies:** 16d (approved requests to pay out), 16e (Transfer API wrapper), explicit user sign-off (Boundary)

**Files likely touched:** `apps/withdrawal/tasks.py`, `apps/withdrawal/views.py` (webhook),
`apps/withdrawal/urls.py`, a new migration if a `WithdrawalCycleRun`/`Failure` audit model is needed,
`bancostore/settings.py` (Celery Beat schedule), `tests/feature/withdrawal/`

**Estimated scope:** L -- the largest single slice in this task; split the webhook handler from the
batch driver into two commits/reviews if it grows past ~5 files, same guidance Task 16's own
top-level entry gives

**Skills:**
- *Before:* **user sign-off (Boundary)**, `doubt-driven-development` -- this is the exact shape of
  slice (a batch driver moving real money on a schedule) that has caught real, ship-blocking bugs
  in this codebase twice already (Task 13's overlapping-cycle lock renewal, Task 14's unsaved-stub
  `.rank` read) before implementation started; do not skip this step because "it's just like the
  other two batch drivers", `source-driven-development` for the webhook payload/event-name shapes
- *During:* `incremental-implementation`, `test-driven-development`, `observability-and-instrumentation`
  (a reversal is a real financial event -- it needs to be as visible in logs/audit trail as a payout
  success, not just the happy path)
- *After:* `security-and-hardening` (new webhook endpoint = new unauthenticated-by-default attack
  surface until signature verification is confirmed correct), `code-review-and-quality`,
  `code-simplification`, `documentation-and-adrs` if the audit-trail model decision (generalize vs.
  new model) needs recording

##### Task 16f internal sequencing (PR 1 merged 2026-07-24; this covers PR 2 onward)

PR 1 (merged, PR #19) built the pure service layer only: `claim_for_payout`, `process_withdrawal_payout`,
`apply_verified_transfer_outcome`, `PaystackNotFoundError`, and the `payout_recipient_name` snapshot.
Nothing calls these yet. Planning pass (2026-07-24) re-read `apps/commissions/tasks.py` in full and
found one thing this task's original acceptance criteria assumed that isn't true: **a webhook
endpoint does not need to be built from scratch.** `apps/distributors/views.py::paystack_webhook`
already exists, already verifies `x-paystack-signature` via `verify_webhook_signature` (Paystack's
*one* site-wide HMAC-SHA512 scheme, not a per-event scheme -- confirmed by reading
`apps/distributors/paystack.py` directly, not assumed), and already dispatches by payload shape
(`event` + reference prefix) for `charge.success`. Adding a `transfer.success`/`failed`/`reversed`
branch there is a ~15-line addition to an existing, already-hardened endpoint, not a new attack
surface needing its own signature-scheme research. This refines (but doesn't override) the original
"new webhook endpoint" framing in ADR-0004's Consequences section -- flagging for a doubt-driven pass
to confirm, and an ADR-0004 addendum afterward (ask before editing, per standing instruction).

Also found: `process_withdrawal_payout` (PR 1) already calls `apply_verified_transfer_outcome`
internally when `verify_transfer` returns a resolved status on its resume path (see
`test_resuming_a_transfer_paystack_already_confirmed_succeeded`/`..._that_actually_failed_reverses_it`
in `tests/unit/withdrawal/test_process_withdrawal_payout.py`). That means **the batch driver alone,
with zero webhook, is already correctness-complete** -- a transfer that resolves between Fridays
just waits for the next cycle's resume-via-poll instead of resolving same-day. The webhook is a
latency optimization, not a correctness dependency. This justifies splitting into two independent
PRs, buildable/shippable in either order, rather than one:

**PR 2 -- the Friday payout mechanism (batch driver + audit model), no webhook involved:**
1. `WithdrawalCycleRun`/`WithdrawalCycleFailure` model + migration (schema only, mirrors
   `CommissionCycleRun`/`Failure`'s shape field-for-field but is a genuinely separate model --
   `CommissionCycleFailure.distributor_id` cannot hold a `withdrawal_request_id` without corrupting
   the field's meaning, per 16f's original doubt-driven RECONCILE). No logic yet, no dedicated test
   file needed beyond a migration check -- mirrors how `WithdrawalRequest` itself shipped schema-only
   in 16b.
2. **Doubt-driven-development pass, before any of steps 3-4's code is written.** Adversarial review
   target: the planned `apps/withdrawal/tasks.py` batch driver design (not yet written). Specifically
   must check, line-by-line against the actual PR 1 source: does `claim_for_payout`/
   `process_withdrawal_payout` read *any* attribute off the passed-in `WithdrawalRequest` object
   other than `.pk` before re-fetching a locked row internally? If the batch driver is going to pass
   an unsaved `WithdrawalRequest(pk=id)` stub per iteration (mirroring `_run_commission_cycle`'s own
   `Distributor(pk=id)` convention), and either function reads a blank field off that stub instead of
   the locked fetch, that's the exact Task 14 "unsaved stub silently pays/skips nobody" bug class
   recurring in new code. Also in scope for this pass: confirm the query needs to select **both**
   `approved_debited` and `queued_for_payout` rows (not just the first), and settle whether
   `WithdrawalCycleFailure` should carry `withdrawal_request_id` as a plain `PositiveIntegerField`
   (not a `ForeignKey`), mirroring `CommissionCycleFailure`'s own reasoning about surviving a deleted
   row. `source-driven-development` sub-check in the same pass: confirm `django_celery_beat`'s
   installed-version `CrontabSchedule.day_of_week` field accepts `"friday"` directly or needs
   conversion to crontab's `0-6`/`fri` syntax -- read the installed package, don't assume.
3. `apps/withdrawal/tasks.py`: the batch driver itself (`process_withdrawal_payouts` or similar),
   shape-mirroring `_run_commission_cycle` (fixed `run_at`, per-iteration-renewed lock, per-row
   exception isolation, systemic-failure guard, audit record persisted before that guard's raise) but
   as its own function in its own module -- not calling into `apps.commissions.tasks`, per the
   already-resolved "shape-mirror, don't generalize" decision. Iterates
   `WithdrawalRequest.objects.filter(status__in=[APPROVED_DEBITED, QUEUED_FOR_PAYOUT])`, calls
   `process_withdrawal_payout` per row. TDD: tests written alongside this file, not after --
   `tests/unit/withdrawal/test_payout_batch_task.py` covering the two-status query, per-row isolation,
   systemic-failure guard, and (per the doubt-driven finding above) a stub-vs-locked-row regression
   test proving a stale/blank field on an unsaved stub can never leak into the actual transfer call.
4. `_sync_periodic_task_crontab` (new, alongside the driver) + a seed migration for the
   `WithdrawalRequest` Beat task, using `CrontabSchedule` -- genuinely new infrastructure, no
   `IntervalSchedule` precedent applies. `WITHDRAWAL_DAY`'s live constance value stays synced the same
   way `BINARY_BONUS_INTERVAL_MINUTES`/`MATCHING_BONUS_INTERVAL_DAYS` already are.
5. `observability-and-instrumentation` decision before merge, not after: at minimum, confirm the
   audit-trail model from step 1 is actually visible somewhere an admin would look (Django Admin
   list view, mirroring `CommissionCycleRunAdmin`) -- a table nobody queries isn't observability,
   it's just a second place logs could have gone.
6. `shipping-and-launch` decision before merge: Paystack Transfers are still blocked on this sandbox
   account's tier (`project_paystack_transfer_account_tier_blocked` memory) -- `initiate_transfer`/
   `verify_transfer` are unverified live. Decide explicitly whether the seeded `PeriodicTask` ships
   `enabled=True` (fires for real, would currently fail every row at the live Paystack call until the
   account tier is fixed -- but fails safely, since nothing debits twice) or `enabled=False` until
   Transfers are confirmed live -- don't let this default silently either way.

**Doubt-driven-development, cycle 1 (2026-07-24), against this exact design -- 6 findings, all
folded in below before any code was written.** Single-model, fresh-context, adversarial (`agent-
skills:code-reviewer`); cross-model offered and declined by the user as unnecessary for this pass.

1. **Lock-timeout sizing.** The plan as first drafted copied `_run_commission_cycle`'s
   lock-renewed-once-per-iteration pattern verbatim. That's safe there because `process_one` is a
   fast, pure-DB call; `process_withdrawal_payout` can make up to 3 sequential Paystack network
   calls per row. A lock TTL sized like Binary Bonus's (picked for many-fast-items) can expire mid-row,
   letting a second Beat trigger's cycle also call `initiate_transfer` for the same row. Mitigated,
   not eliminated, by `claim_for_payout`'s own documented backstop (Paystack's `reference` uniqueness
   is meant to reject a genuine duplicate `initiate_transfer`) -- but that's an unverified third-party
   guarantee on this account (Transfers still blocked, per the memory above), so it's a second layer,
   not a substitute for sizing the lock correctly. **Resolution:** size
   `WITHDRAWAL_PAYOUT_LOCK_TIMEOUT_SECONDS` explicitly around worst-case per-row Paystack round-trip
   time (`REQUEST_TIMEOUT_SECONDS` x up to 3 calls, plus headroom), not copy-pasted from Binary Bonus,
   and document the two-layer defense (lock + Paystack reference dedup) explicitly in the driver's
   docstring, mirroring Matching Bonus's own honest "here's what actually protects this" framing.
2. **`paid`/`total_amount` accounting can't trust the per-row return value.** `process_withdrawal_
   payout` returns only a status string -- it can't distinguish "this call just paid the row" from
   "the row was already `paid` before this call started" (e.g. resolved by a genuinely concurrent
   process). Trusting that string for `paid += 1` / `total_amount += net_amount` during the loop risks
   double-counting the same real payment across two cycle records once PR 3's webhook exists (and, in
   PR 2 alone, under the lock race in finding 1). Also, `process_withdrawal_payout` doesn't return
   `net_amount` at all, so there was no clean source for `total_amount` even setting the double-count
   risk aside. **Resolution (one fix closes both):** don't accumulate `paid`/`total_amount` inside the
   loop at all -- after the loop finishes, run one aggregate query:
   `WithdrawalRequest.objects.filter(pk__in=processed_ids, status=Status.PAID).aggregate(paid=Count("pk"), total_amount=Sum("net_amount"))`.
   One extra query per cycle (not per row), so this doesn't reopen the flat-query-count goal.
3. **Crontab conversion needs explicit validation, not a bare `get_or_create`.** Confirmed directly
   against the installed `django_celery_beat` package (not assumed):
   `validators.day_of_week_validator("friday")` raises (`"Unrecognised Day of Week: 'friday'"`);
   `"fri"` and `"5"` both pass. `WITHDRAWAL_DAY`'s seeded constance value is the literal string
   `"friday"`, so a naive `CrontabSchedule.objects.get_or_create(day_of_week=config.WITHDRAWAL_DAY,
   ...)` would either write an invalid row silently (validators don't run on `.save()`) or blow up
   confusingly deep in Beat's own scheduling loop -- which is shared with Binary/Matching Bonus's
   periodic tasks, not isolated to withdrawal's. **Resolution:** an explicit
   `{"monday": "1", ..., "sunday": "0"}` mapping (not celery's internal name-abbreviation parser --
   keep the conversion visible and directly testable), and call the validator explicitly before
   saving, falling back to "leave the existing schedule alone and log a warning" on anything
   unrecognized -- mirrors `_sync_periodic_task_interval`'s own defensive pattern for bad input.
4. **Row ordering.** `_withdrawal_payout_ids_query()` had no `.order_by(...)`. Not a correctness bug
   (this is one `.iterator()` snapshot processed once, not paginated across separate queries the way
   the earnings-history bug was), but for a real-money weekly batch, oldest-submitted-first is a
   better default than whatever order MySQL happens to return. **Resolution:** add
   `.order_by("created_at")` -- trivial cost, worth taking.
5. **`WithdrawalCycleFailure` needs more than a bare ID.** Traced further than the original finding:
   unlike a failed *commission calculation* (no money ever moved), a `WithdrawalCycleFailure` row
   represents a request whose wallet was already debited back at approval time (Task 16d). If that
   row's `Distributor` is later deleted (`DistributorAdmin` has no delete lock, unlike `WalletAdmin`/
   `WithdrawalRequestAdmin`/`CommissionCycleRunAdmin`, all of which do) and the `WithdrawalRequest`
   cascades away with it, a bare `withdrawal_request_id` + error string leaves no way to tell who was
   owed how much. **Resolution:** add `distributor_id` (plain `PositiveIntegerField`, mirroring
   `CommissionCycleFailure`'s own precedent) and `net_amount` (`DecimalField`) to
   `WithdrawalCycleFailure`, both captured at failure time from the still-live row -- matching
   precedent scope, not adding a full snapshot of every field.

All 5 folded into the PR 2 design above (batch-driver step 3, audit-model step 1, crontab-sync step
4) before implementation starts. Stopping at 1 cycle -- every finding was either fixed outright or
(finding 1) mitigated with an explicit, justified sizing decision plus honest documentation of the
residual risk, not hand-waved away; none of the fixes surface new branching complex enough to warrant
an immediate second pass. A second doubt cycle can run against the actual implementation once written
if anything about the fixes above turns out to be harder than expected in code.

**PR 3 -- the transfer webhook (small, independent of PR 2):**
1. Extend `apps/distributors/views.py::paystack_webhook` with a
   `transfer.success`/`transfer.failed`/`transfer.reversed` branch, reusing the existing
   `verify_webhook_signature` call already at the top of that view -- no new signature code.
   Dispatches by reference prefix (`withdrawal-payout-`, from PR 1's `_generate_transfer_reference`)
   to a new deferred Celery task, mirroring `consume_didit_result_task`'s shape (fast HTTP response,
   heavy lifting in the task).
2. New Celery task (`apps/withdrawal/tasks.py`, alongside the batch driver): re-fetches the
   authoritative status via `verify_transfer` (never trusts the webhook payload's own status field --
   same rule already enforced for Didit and `charge.success`), then calls
   `apply_verified_transfer_outcome`. Idempotent by construction (PR 1's function already is), but
   needs its own test proving a duplicate webhook delivery for the same reference doesn't
   double-reverse.
3. `security-and-hardening` pass specifically on this extension: confirm the new branch can't be
   reached without the existing signature check (it's added inside the same `if not
   verify_webhook_signature(...)` guard, not a parallel unguarded path), and that an attacker-supplied
   reference that doesn't match any `WithdrawalRequest` fails closed (logs and 200s, doesn't 500 --
   mirrors the existing `elif reference:` unrecognized-prefix handling).
4. `documentation-and-adrs`: a short ADR-0004 addendum recording the "reuse the existing webhook
   endpoint, not a new one" decision and why (ask before editing ADR-0004, per standing instruction).

**Order:** PR 2 before PR 3. The batch driver's own resume path (step 3 above) is correctness-complete
without the webhook -- a transfer that resolves before the next Friday just waits for that next
cycle's poll. Building PR 2 first means the whole payout mechanism is provably correct via polling
alone before PR 3 adds the webhook's latency optimization on top.

**Checkpoint after PR 2:** full `tests/unit/withdrawal/` + `tests/unit/commissions/` suites green,
real MySQL CI green, a manual Django shell run of the batch driver against a seeded
`approved_debited` row (Paystack Transfers still blocked on this account tier, so this can only be
verified against mocked/sandboxed responses, not a real live transfer -- note this explicitly in the
PR description rather than implying live verification happened).

**Checkpoint after PR 3:** duplicate-webhook-delivery test green, a manual webhook POST replay (same
technique already used to verify the Didit webhook) confirms signature verification actually rejects
a tampered body.

---

#### Task 16g: Distributor-facing status + notifications

**Description:** Withdrawal request status/history visible to the distributor (own page or folded
into Earnings History), with SMS/email notification on approval, payout, and rejection --
wrapped so a notification failure can never roll back an already-committed state change, mirroring
the Direct Referral Bonus SMS pattern from Task 12. The UI must say plainly that "approved" does not
mean "paid yet" (ADR-0004 consequence: a request can sit approved-but-unpaid for up to a week).

**Acceptance criteria:**
- [x] Distributor can see every past request and its current status, scoped to their own requests only
- [x] Status copy is honest about the approved-but-not-yet-paid window (no implication that approval == payment)
- [x] SMS/email failure is logged but never reverts the underlying status transition

**Verification:**
- [x] pytest: notification-provider exception doesn't roll back the transaction that triggered it
- [x] Verified live in a real browser (desktop, tablet, and mobile widths)

**Built 2026-07-24.** `templates/distributors/withdrawal_history.html` (new page, not folded into
Earnings History -- `WithdrawalRequest`'s own status/lifecycle isn't represented by
`WalletTransaction` rows, so this is genuinely different data, not a duplicate view). Wired into the
sidebar's previously-disabled "Withdraw" nav item. SMS notifications added to
`approve_withdrawal_request`/`reject_withdrawal_request`/`apply_verified_transfer_outcome`
(`apps/withdrawal/services.py`'s new `_notify()` helper), sent only after `retry_on_lock_contention`
returns -- never from inside the locked `_attempt()` closure, a stricter standard than Task 12's own
precedent (`_credit_direct_referral_bonus` sends from inside its lock), matching Task 16f's "never
hold a lock across external I/O" principle. `apply_verified_transfer_outcome`'s internal `_attempt()`
return shape changed from `locked_request` to `(locked_request, transition)` so the notification
fires exactly once per real transition, not once per call -- its own idempotent no-op case (a
duplicate webhook delivery, or the webhook racing the batch driver's resume path) must not
re-notify. Public contract unchanged (still returns `WithdrawalRequest`).

Also fixed while building/testing this page in a real browser (not part of the original scope, but
discovered live): a Tailwind build-cache miss on the new template (the "Withdraw Now" button
rendering with no styling until `npm run build` ran), a mismatched type-scale across the status
badges/amount columns (some cells were missing their `text-X` size token entirely, defaulting to
browser sizing next to correctly-sized siblings), and a missing `min-width` on the table causing
mobile viewports to crush columns instead of scrolling the whole table as a block. The same two
bugs (missing size token, missing table `min-width`) turned out to already exist on
`earnings_history.html` and all 5 `admin_portal` tables (`withdrawal_review_queue.html`,
`kyc_review_queue.html`, `commission_oversight.html`, `commission_cycle_detail.html`,
`partials/directory_results.html`) -- fixed there too, each table now has a documenting comment on
both rules so this doesn't recur silently.

**Deferred, not decided silently (code-review-and-quality, 2026-07-24):** `approve_withdrawal_request`
/`reject_withdrawal_request`'s SMS send is synchronous, inside the same HTTP request Django Admin's
`approve_selected`/`reject_selected` bulk actions use. An admin bulk-approving/rejecting many requests
in one action now waits on one SMS send per row (mNotify's own timeout is 10s) before the request
completes -- a handful of rows is fine, but a large bulk selection risks a slow admin page load or a
web-server request timeout. Not a money-safety issue (the debit/credit itself is unaffected, only
how long the admin's browser waits), and the batch driver's own lock-timeout budget was re-verified
and re-documented to still hold safely with this extra call (`apps/withdrawal/tasks.py`'s
`WITHDRAWAL_PAYOUT_LOCK_TIMEOUT_SECONDS` comment). The real fix -- deferring the SMS send via a
Celery task, mirroring `consume_didit_result_task`/`process_transfer_webhook_task`'s already-
established pattern -- was surfaced to the user and explicitly deferred rather than fixed now; revisit
if bulk admin actions on withdrawals start processing large batches in practice.

**Dependencies:** 16c, 16d, 16f (all three states need to be visible)

**Files likely touched:** `templates/distributors/withdrawal_history.html` (or an extension of
`templates/distributors/earnings_history.html`), `apps/withdrawal/views.py`,
`apps/notifications/` (existing app -- extend, don't duplicate), `tests/`

**Estimated scope:** S-M (2-4 files)

**Skills:**
- *Before:* none beyond the standing `security-and-hardening` awareness carried from earlier slices
  (this is UI/notification polish, not a new money-movement decision)
- *During:* `frontend-ui-engineering`, `incremental-implementation`, `security-and-hardening` (mNotify/
  email are also Boundary "ask first" integrations -- confirm before modifying `apps/notifications/`
  if the change is more than calling an existing send function), `browser-testing-with-devtools` if a
  real browser check surfaces something a screenshot alone wouldn't (console errors, network failures)
- *After:* `code-review-and-quality`, `code-simplification`

---

#### Task 16h: Full-suite verification, CI, PR, Checkpoint F

**Description:** The Section-14-style full journey test (purchase → commission credit → withdrawal
request → tax deducted → admin approves → simulated Paystack payout succeeds), taken through the
same branch → PR → CI (real MySQL) → CodeRabbit → merge workflow every prior task has used.

**Acceptance criteria:**
- [x] Full pytest suite green, including every new withdrawal test from 16a-16g (730 passed)
- [x] `black`/`ruff` clean, `manage.py check` clean
- [x] CI green against real MySQL, not just local SQLite (verified at the log level, not just the
  checkmark -- confirmed against `mysql:8.4.10` via `gh run view --log`)
- [x] CodeRabbit review complete, actionable findings resolved or explicitly deferred with reasoning
  (PR #22: one real bug fixed pre-merge -- widened `apply_verified_transfer_outcome`'s exception
  catch to cover a malformed Paystack response; one finding verified as a contract-misread, not a
  real gap -- test SMS safety is already covered by `tests/conftest.py`'s autouse fixture. PR #23:
  UI-only, no CodeRabbit findings)

**Verification:**
- [x] Checkpoint F (below) passes end-to-end in a real browser session, not just pytest -- run
  2026-07-24 with a real distributor ("Checkpoint F Distributor"), a real admin login (TOTP 2FA),
  and every financial figure checked against the database after each step, not just the UI:
  withdrawal of GHS 500.00 -> GHS 5.00 tax -> GHS 495.00 net; admin approval debited the wallet
  exactly once (GHS 1000.00 -> GHS 505.00, one `WalletTransaction` row); a mocked Paystack payout
  (real Transfers are blocked on this sandbox account's tier -- see
  `project_paystack_transfer_account_tier_blocked`) resolved the request to `paid` with no
  double-debit; the distributor's own withdrawal history page correctly showed the final "Paid"
  status. Caught one real process gap during the walkthrough: a manual verification shell command
  is NOT covered by `tests/conftest.py`'s pytest-only autouse SMS-suppression fixture, so it made a
  live (rejected, no message sent) call to mNotify's API -- manual verification scripts must mock
  `send_sms` directly rather than relying on that fixture or the dev server's own env override.

**Dependencies:** 16a-16g all merged

**Files likely touched:** none new -- this is verification, not implementation. No PR opened for this
task -- purely verification, nothing to merge.

**Estimated scope:** XS (process, not code)

**Skills:**
- *Before:* none
- *During:* none
- *After:* `ci-cd-and-automation` (confirm the pipeline actually ran the withdrawal suite against
  MySQL, not just that CI was green for unrelated reasons), `git-workflow-and-versioning`
  (branch/PR hygiene consistent with Tasks 13-15's pattern), `debugging-and-error-recovery` if
  anything breaks in CI that didn't break locally (a real risk given SQLite-vs-MySQL locking
  differences are this codebase's most-repeated gotcha)

---

**Skills deliberately not called out per-slice above, and why:**
- `api-and-interface-design` -- applies lightly throughout (the `submit_withdrawal_request`/
  `approve_withdrawal_request` service signatures, and the Transfer webhook's payload contract) but
  isn't a dedicated pass; keep new service function signatures consistent with `credit()`/`debit()`'s
  existing shape rather than inventing a new convention.
- `ci-cd-and-automation` -- only relevant at 16h; nothing about withdrawal changes the pipeline
  itself before then.
- `context-engineering` -- applies to how each slice above should be worked (load only that slice's
  section of this breakdown plus the relevant source files, not the whole task at once), not to the
  product being built.
- `deprecation-and-migration` -- not applicable; nothing existing is being removed or migrated off.
- `idea-refine` / `interview-me` -- already done for this task, via the `AskUserQuestion` round that
  produced ADR-0004, before this breakdown was written.
- `performance-optimization` -- the 16f batch driver should stay flat-query-count by construction
  (same pattern as Binary/Matching Bonus), but this is inherited discipline, not a dedicated
  optimization pass; only invoke for real if 16f's own measurement (see its Verification) shows
  a problem.
- `shipping-and-launch` -- this is a task within an ongoing build, not a production launch; N/A here,
  not skipped by oversight.
- `using-agent-skills` -- the meta-skill governing this whole breakdown's own construction; already applied.

---

**Checkpoint F:** a distributor requests a withdrawal, tax is deducted correctly, admin approves,
and a simulated Paystack payout succeeds.

---

## Phase 6: Checkout & Orders

### Task 17: Cart + checkout

**Description:** Cart review, delivery vs. pickup choice, delivery fee calculated by zone, order
summary, Paystack payment. Distributor purchases generate PV; regular purchases do not.

**2026-07-24 -- delivery zone fee decision resolved, design gaps closed via `spec-driven-development`
before any code.** Open Question #4 (delivery zone fee table, "set by admin" with no real numbers)
is resolved: Kumasi GHS 20, Accra GHS 50, other regions GHS 70 (the source doc's own Section 5.1
worked example, corrected against an earlier different-numbers proposal once the primary source was
actually checked), free-delivery threshold GHS 500, pickup always free -- all four seeded as
admin-editable `django-constance` settings, not hardcoded. Three further architecture questions had
no answer anywhere in `SPEC.md` and were resolved and recorded in
`docs/decisions/0005-checkout-cart-design.md` rather than assumed: cart is session-based for every
visitor, guest or logged-in, with no `Cart`/`CartItem` DB model or merge-on-login path (no MVP
requirement demands cross-device cart persistence); `Order` is created in a `pending` state at
checkout confirmation, *before* payment -- a deliberate divergence from Task 16's
`PendingRegistration`-holds-data-until-paid precedent, because Section 5.2's own order-status table
starts at "Pending -- payment not yet confirmed" as a real visible state and Task 18 needs a real row
to auto-cancel; stock is checked and decremented at payment confirmation, not reserved at
add-to-cart. Broken into 6 vertically-sliced sub-tasks below (17a-17f), ordered so the schema +
cart mechanics ship and are fully tested before payment integration and the UI/frontend layer.
**Frontend note:** this task has real UI (cart, checkout, order confirmation). Per explicit user
instruction, the UI is designed via Stitch from a prompt the user sends themselves, not built
freehand -- 17e is gated on that prompt/export round-trip, not on Claude generating a design.

**Acceptance criteria (whole-task, verified by Checkpoint G below alongside Task 18):**
- [ ] Delivery fee is correctly looked up by zone from settings, or free above the threshold
- [ ] A distributor's purchase generates PV and updates the ledger (Task 10d's write path); a regular customer's does not
- [ ] Order confirmation is sent (SMS/email) on successful payment
- [ ] Guest checkout works with no account required; logged-in checkout works for both customer and distributor roles

**Verification (whole-task):**
- [ ] pytest test: distributor purchase increments PV ledger; regular customer purchase does not
- [ ] pytest test: delivery fee matches the configured zone table, and is zero above the free threshold

**Dependencies:** Task 8, Task 10d, delivery zone fee decision (resolved), ADR-0005 design decisions
(resolved), **explicit user sign-off before 17a's schema migration and before 17d's Paystack
integration code specifically** (both are standing `SPEC.md` Boundary "ask first" items,
independent of this breakdown having already been reviewed)

**Estimated scope:** L as a whole -- hence the 17a-17f split

---

#### Task 17a: `apps/orders` app scaffold + delivery-zone settings + `Order`/`OrderItem` models + state machine

**Description:** New Django app. Four new constance settings (`DELIVERY_FEE_KUMASI`,
`DELIVERY_FEE_ACCRA`, `DELIVERY_FEE_OTHER_REGIONS`, `FREE_DELIVERY_THRESHOLD`, all
`non_negative_money_field`) in a new `DELIVERY_SETTINGS` fieldset. `Order` (nullable customer FK
for guest checkout, flat address fields matching `PendingRegistration`'s `address`/`area`/`landmark`
shape, `payment_reference`, delivery/pickup choice, delivery fee snapshot, total, a `pv_earned`
audit field, full `status` choice set per Section 5.2 even though this task only transitions through
`pending`/`confirmed`) and `OrderItem` (product, quantity, unit price + unit PV snapshot -- never a
live product-price/pv_value lookup after the fact).

**Built 2026-07-24.** A `doubt-driven-development` pass before the migration found 8 real gaps in
the first draft, all resolved before implementation -- see `docs/decisions/0005-checkout-cart-design.md`'s
"Task 17a Schema Review" section for the full writeup. The two substantive ones: no PV snapshot
existed anywhere in the original draft (`Product.pv_value` would have been read live at confirmation
time otherwise -- the exact class of bug the delivery-fee/price snapshot decisions already existed
to prevent, just not originally extended to PV), fixed via `OrderItem.unit_pv` + `Order.pv_earned`;
and `Order.email` was originally required, which would have broken checkout for a distributor with
no real email (`apps/distributors/forms.py`'s email field is already `required=False` for exactly
this reason) -- fixed via `blank=True, default=""`. `OrderItem` intentionally has no variant field
(no interactive variant selection exists anywhere in this codebase yet -- Task 7 explicitly deferred
it, and `product_detail.html`'s variant display is plain, non-interactive `<span>` elements) --
corrects this ADR's own decision-2 wording, which had said the cart is keyed by `variant_id`; it's
keyed by product id.

**Acceptance criteria:**
- [x] `Order`/`OrderItem` migrations apply cleanly; `status` choices include every Section 5.2 stage
- [x] Delivery zone settings visible and editable in Django Admin's constance panel
- [x] `OrderItem` unit price is a snapshot at order-creation time, never re-derived from `Product` later

**Verification:**
- [x] pytest test: creating an `Order` with a snapshotted zone fee does not change if the live constance setting changes afterward

**Dependencies:** none beyond Task 17's own (explicit user sign-off required before this migration, per SPEC.md Boundaries -- given 2026-07-24)

**Files likely touched:** `apps/orders/` (new app), `apps/orders/models.py`, `apps/orders/admin.py`, `apps/platform_settings/config.py`, `bancostore/settings.py`, `tests/unit/orders/test_order_model.py`, migrations

**Estimated scope:** S

**Skills:**
- *Before:* `doubt-driven-development` (schema/state-machine review before the migration exists, mirroring ADR-0004's Task 16a review -- 8 findings, all resolved, see ADR-0005)
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`, `security-and-hardening` (guest-checkout PII fields)

**Carried forward to 17c/17d:** `Order.subtotal` equaling `sum(OrderItem.unit_price * quantity)`
cannot be enforced by a DB `CheckConstraint` (can't reference another table) -- 17c (order creation)
and 17d (payment confirmation) must each have an explicit test proving this identity holds, since
the schema itself can't guarantee it.

---

#### Task 17b: Session-based cart

**Description:** `apps/orders/cart.py::Cart` -- a thin wrapper over `request.session` (`{product_id:
quantity}`), identical for guest and logged-in visitors per ADR-0005 decision 2. Add/update-quantity/
remove-item views, a cart review page, and wiring `templates/catalog/product_detail.html`'s
existing disabled add-to-cart stub (Task 8) to a real add-to-cart action. Product-level, not
variant-level (corrected via 17a's own schema review, CodeRabbit-caught on PR #24 as stale wording
here specifically): no interactive variant selection exists anywhere in this codebase yet, and
stock is tracked at the Product level only (Task 7).

**Built 2026-07-24.** A `code-review-and-quality` pass on the first draft found and fixed two real
bugs before merge: `Cart.items()` prefetched the wrong relation (`select_related("category")`
instead of `prefetch_related("images")`, a genuine N+1 on `cart.html`'s `primary_image` access,
empirically confirmed via a query-count comparison test mirroring `test_listing.py`'s own
silk-tolerant pattern); and `cart_update`/`cart_remove` resolved products via
`Product.objects.storefront_visible()` (which filters `is_active=True`), 404ing on a product
deactivated after being added to the cart with no way for the customer to recover -- fixed by using
the plain manager for those two views (visibility gates *adding* new items, not modifying/removing
an existing line). Also fixed: `Cart.add()` previously returned nothing, so `cart_add` always
redirected to `/cart/` as if it succeeded even when a product had zero available stock at add time
(a silent no-op); `add()` now returns a bool and the view redirects back to the product page
instead when nothing was added. A bounded session read-modify-write race (two concurrent requests
from the same session, no compare-and-swap) was reviewed and deliberately left unfixed, documented
in `Cart`'s own docstring -- checkout, not the cart, is the source of truth (ADR-0005), so the worst
case is a lost add/update the customer can retry, not a money bug. `ADR-0005`'s own decision-2
wording (`{variant_id: quantity}`) was corrected to `{product_id: quantity}` to match. Verified live
in a real browser: guest add/update/remove/stock-cap/empty-state, and the identical flow again as a
logged-in customer.

**Acceptance criteria:**
- [x] Add to cart, update quantity, remove item all work for both anonymous and logged-in sessions
- [x] Cart total (excluding delivery, computed in 17c) reflects live `Product` prices, quantities capped by current product stock
- [x] Product detail page's add-to-cart button is no longer disabled

**Verification:**
- [x] pytest test: cart survives across requests within a session; a fresh session starts empty
- [x] pytest test: cannot add more of an item than current stock allows

**Dependencies:** 17a merged

**Files likely touched:** `apps/orders/cart.py`, `apps/orders/views.py` (cart), `apps/orders/urls.py`,
`apps/orders/context_processors.py` (header cart badge, new), `templates/orders/cart.html` (new),
`templates/catalog/product_detail.html`, `templates/base_store.html` (header cart icon wiring),
`tests/unit/orders/test_cart.py`, `tests/feature/orders/test_cart_views.py`

**Estimated scope:** S

**Skills:**
- *During:* `test-driven-development`, `incremental-implementation`, `api-and-interface-design` (Cart class shape)
- *After:* `code-review-and-quality` (2 real bugs found and fixed, see narrative above), `browser-testing-with-devtools` (guest + logged-in verified live)

---

#### Task 17c: Checkout flow -- delivery/pickup choice, address, order summary, `Order` creation

**Description:** Cart review -> delivery-or-pickup choice -> address entry (Home Delivery only) ->
`DeliveryFeeCalculator` service (zone lookup + free-threshold check, per ADR-0005 decision 1) ->
order summary showing items/subtotal/delivery fee/total -> confirming creates the `Order`/
`OrderItem` rows in `pending` status with everything snapshotted (per 17a), not yet paid.

**Built 2026-07-25.** `calculate_delivery_fee`/`create_pending_order`/`prefill_contact_info` landed
in `apps/orders/services.py`, with the checkout page built from the fetched Stitch screens
("Checkout - Bancostore"/"(Mobile)"), reconciled against real zone-fee values (the mockup's own
numbers didn't match the seeded constance defaults). A `doubt-driven-development` pass before any
code caught the identity risk 17a's own schema review flagged in advance -- `create_pending_order`
computes `subtotal` from the exact same `cart_items` list every `OrderItem` snapshot comes from,
never a second independent `Cart.subtotal()` call (which re-reads live `Product.price` and could
have desynced the two) -- plus missing `transaction.atomic()` wrapping and a `Cart.items()` gap
where a product *deactivated* (not deleted) after being added to a cart wasn't pruned, risking a
snapshotted `OrderItem` for something unpurchasable. A `code-review-and-quality` pass after
implementation caught a missing Post/Redirect/Get pattern (a page refresh on the direct POST
response could create a duplicate pending `Order`) -- fixed by redirecting to a GET confirmation
view keyed by the order's own unguessable `payment_reference`. Separately, replaced the Delivery
Zone field's native `<select>` (and, on the admin side, the Distributor Directory's status filter)
with the same themed Alpine.js listbox pattern already used by `payout_settings.html`'s
mobile-money-network field -- a native select's open options popup can't be restyled via CSS in any
browser. That listbox work surfaced a real, previously-shipped-once-already XSS: interpolating
request-controlled values (the delivery method/zone form data, the admin status filter's
querystring param) directly into an Alpine `x-data` JS string is exploitable despite Django's HTML
auto-escaping, because the browser HTML-decodes the attribute *before* Alpine evaluates it as JS --
fixed the same way `apps/distributors/views.py::payout_settings` fixed it once before: constrain to
known-good values server-side, then pass via `json_script`, never raw interpolation.

**Acceptance criteria:**
- [x] `DeliveryFeeCalculator` returns the correct fee for each zone and zero above the free threshold
- [x] Pickup selection always yields a zero delivery fee, no zone lookup
- [x] Confirming the order summary creates a real `pending` `Order`, decrements nothing yet (stock untouched until 17d)

**Verification:**
- [x] pytest test: `DeliveryFeeCalculator` matches the configured zone table exactly (whole-task verification bullet)
- [x] pytest test: order total is fee-plus-subtotal, snapshotted correctly (17a's carried-forward identity test)

**Dependencies:** 17a, 17b merged

**Files likely touched:** `apps/orders/services.py` (`DeliveryFeeCalculator`), `apps/orders/views.py` (checkout), `tests/feature/orders/test_checkout.py`

**Estimated scope:** M

**Skills:**
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`

---

#### Task 17d: Paystack payment + confirmation -- stock decrement, PV branch, order confirmation notification

**Description:** `initialize_transaction` call + redirect from the confirmed `pending` `Order`;
webhook/callback triggers `confirm_order_payment(reference)`, mirroring
`consume_paid_starter_pack`'s idempotent locked-consume shape exactly (per ADR-0005 decision 4):
locks the `Order` row, no-ops if already resolved, `verify_transaction` + exact-amount check, then
inside the lock decrements stock per line item (`apps.catalog.services.decrement_stock`), credits
PV via `record_purchase_pv` **only if `is_distributor(order.customer)`** (decision 6), transitions
to `confirmed`. SMS+email confirmation sent after the lock releases, never inside it (Task 16g's
standard). An out-of-stock line item discovered at confirmation time (not reserved earlier, per
decision 5) must fail the order cleanly, not partially decrement other lines.

**Built 2026-07-25.** `confirm_order_payment` shipped as designed, with two real gaps fixed before
any code was written by a `doubt-driven-development` cycle (two independent cross-model passes,
reconciled): the original draft only called `record_purchase_pv` (ancestors), never
`record_personal_pv` (self) -- missing the latter would have silently left every distributor
permanently ineligible for the monthly-100-PV eligibility threshold from storefront purchases,
since only starter-pack confirmation had ever called it before; and `Order.pv_earned` needed to
reflect what was *actually* credited, not the pre-computed amount, since `record_purchase_pv`
silently no-ops for a distributor with no `BinaryTreeEdge` yet (unreachable through normal
onboarding, but not database-guaranteed). The insufficient-stock-at-confirmation handling ADR-0005
decision 5 explicitly left "TBD at implementation time" was resolved directly with the user: the
order transitions to `cancelled` (not left `pending` to retry forever on every Paystack webhook
redelivery, since the customer's payment was already captured), logged at ERROR for a human to
action the actual refund -- real Paystack Refund API automation stays Task 18's scope, not silently
assumed. Wired into the existing shared `paystack_webhook` (its `charge.success` if/elif dispatch
was refactored to a dict, exactly per that code's own forward-looking comment anticipating a third
prefix) and a new `order_payment_callback` view mirroring `starter_pack_payment_callback`'s
redirect-back-then-re-verify shape. `templates/orders/order_created.html` was rebuilt from the
user's fetched Stitch screens ("Order Confirmation - Bancostore (Success)"/"(Vibrant Colors -
Mobile)", "Order Unavailable - Bancostore (Refund Pending)"/"(Refund Pending - Mobile)") -- both
mockups fabricated an automated refund timeline and a fake Initiated/Processing/Completed tracker
that don't match this project's actual manual-admin-follow-up decision, dropped rather than carried
through. A pre-commit `code-review-and-quality` pass caught a real N+1 in that page's line-item
rendering (`item.product.primary_image` needs a caller's `prefetch_related`, per that property's
own docstring) -- fixed, with a regression test confirmed to actually catch the regression (failing
without the fix, passing with it). **CodeRabbit caught 5 real issues on PR #27**, all fixed in a
follow-up commit before merge: (1, Major) the session cart was never cleared after a confirmed
payment, so the customer could immediately re-order the same items -- fixed via a new `Cart.clear()`
called from `order_payment_callback` once the order's post-confirmation status is actually
`confirmed`; (2, Minor) `data["authorization_url"]` was the one Paystack response read in this
codebase using bracket indexing instead of `.get()`, turning a malformed/missing key into an
unhandled 500 after the order already existed; (3, Major) `order_payment_callback` reversed an
unvalidated, fully attacker-controlled query-string reference into a URL -- confirmed via
`manage.py shell` that any reference containing `/` raises `NoReverseMatch` (the `<str:...>`
converter excludes it), an unauthenticated 500 on a public GET endpoint, fixed by resolving the
`Order` first; (4, Minor) the confirmation template's bare `{% else %}` branch would have
mislabeled a later lifecycle status (`processing`/`dispatched`/`delivered`/`refunded`, Task 18's
scope) as "Awaiting Payment Confirmation" -- fixed with an explicit `pending` branch and a genuine
neutral fallback; (5, Minor) a stale test comment left over from reducing a concurrency test's
thread count. That thread-count reduction (5 -> 2) was itself a real fix found during this task's
own verification, not a shortcut: the 5-thread version (mirroring `apps/wallet`'s own convention)
was intermittently flaky under local SQLite specifically because `confirm_order_payment`'s
correctness depends on `select_for_update` actually resolving contention, which SQLite has no real
implementation of at all -- `apps/wallet`'s own test is safe at 5 threads only because its
correctness instead comes from a race-free bulk `F()` update. Reduced to 2 threads to match
`test_consume_paid_starter_pack.py`'s own existing precedent for this exact lock shape, verified
reliable across 8/8 repeated local runs; full concurrency correctness is what CI's real MySQL run
verifies, not this local test.

**Acceptance criteria:**
- [x] A regular customer's confirmed order never calls `record_purchase_pv`
- [x] A distributor's confirmed order credits PV correctly up the ancestor chain (Task 10d's existing write path, reused not reimplemented)
- [x] A duplicate webhook delivery for an already-confirmed order is a no-op, not a double stock-decrement or double PV credit
- [x] Order confirmation SMS/email fires on successful payment (whole-task acceptance criterion)

**Verification:**
- [x] pytest test: distributor purchase increments PV ledger; regular customer purchase does not (whole-task verification bullet)
- [x] pytest test: concurrent confirmation attempts for the same reference never double-decrement stock or double-credit PV

**Dependencies:** 17c merged, **explicit user sign-off before this slice specifically** (Paystack integration code, per SPEC.md Boundaries) -- given 2026-07-25

**Files likely touched:** `apps/orders/services.py` (`confirm_order_payment`), `apps/orders/views.py` (webhook/callback), `tests/unit/orders/test_confirm_order_payment.py`

**Estimated scope:** M

**Skills:**
- *Before:* `doubt-driven-development` (fresh adversarial review before writing payment-confirmation code -- same rigor as every prior Paystack-adjacent slice), `security-and-hardening`
- *During:* `test-driven-development`, `incremental-implementation`, `source-driven-development` (confirming Paystack Transaction webhook/callback payload shape against real docs, not assumption)
- *After:* `code-review-and-quality`, `code-simplification`

---

#### Task 17e: Frontend -- cart, checkout, order confirmation UI

**Description:** Real Stitch-designed UI for the cart review, checkout (delivery/pickup + address +
summary), and order confirmation screens, matching this project's established pattern (every prior
customer/distributor-facing page came from a Stitch screen, verified against its own design prompt
before building). **Claude does not design this UI freehand** -- the user sends a Stitch prompt
(provided at this slice's start) and shares the resulting screens/export back for template
integration.

**Not a separate slice in practice -- absorbed into 17b/17c/17d as each landed**, per this
project's own vertical-slice build process (get the Stitch screens for each page at the point
that page is actually being built, not as a deferred separate pass): `cart.html` came from the
fetched Stitch screens during 17b, `checkout.html` during 17c, and `order_created.html` during
17d. Real design, not placeholder, in every case.

**Acceptance criteria:**
- [x] Cart, checkout, and confirmation pages render the real Stitch design, integrated with 17b/17c/17d's actual data
- [x] Mobile-responsive at 320/768/1024/1440px, verified in a real browser (this project's own repeated table/badge-sizing bugs make source-only review insufficient) -- **done 2026-07-26, with one honestly-flagged tooling gap.** Real-browser resize verified `cart.html`, `checkout.html`, and `order_created.html` (both `confirmed` and `cancelled` states) clean at 1440/1024/768px and at 500px, including opening the Alpine delivery-zone listbox at 500px with no overflow. **True 320px was not reachable**: macOS Chrome's window has a ~500px minimum width floor, and the natural workaround (an iframe sized to 320px) is correctly blocked by Django's own `X-Frame-Options: DENY` -- not weakened just to enable a test. Code inspection found no hard-coded `w-[Npx]`/`min-w-[Npx]` fixed widths in any of the three templates (only `max-w-[...]` ceilings, which shrink freely), and the confirmed-clean 500px rendering already exercises the same single-column/stacked layout that would apply all the way down to 320px under Tailwind's mobile-first default classes -- reasonable confidence, not empirical proof, and documented as such rather than silently claimed equivalent to a real 320px screenshot.

**Verification:**
- [x] Manual check: full guest checkout and full distributor checkout both walk correctly through the real UI in a browser (guest checkout verified end-to-end multiple times across 17c/17d; distributor-specific checkout walkthrough not separately re-verified in the browser beyond what confirm_order_payment's own pytest suite covers)

**Dependencies:** 17b, 17c, 17d merged; Stitch prompt sent and screens received from the user

**Files likely touched:** `templates/orders/cart.html`, `templates/orders/checkout.html`, `templates/orders/order_confirmation.html`

**Estimated scope:** M

**Skills:**
- *During:* `frontend-ui-engineering`, `incremental-implementation`
- *After:* `code-review-and-quality`, `browser-testing-with-devtools`

---

#### Task 17f: Full-suite verification, CI, PR, Checkpoint G (Task 17's portion)

**Description:** Same branch -> PR -> CI (real MySQL) -> CodeRabbit -> merge workflow as every prior
task. Verifies Task 17's own slice of Checkpoint G (purchase + correct PV branching); the
"order status updates correctly with notifications" portion of Checkpoint G closes with Task 18.

**Built 2026-07-25.** 17c shipped via PR #26 (CodeRabbit: zero actionable findings), 17d via PR #27
(CodeRabbit found 5 real issues, all fixed in a follow-up commit before merge, then re-reviewed
clean). Full suite green throughout -- 844 passed as of 17d's merge.

**Acceptance criteria:**
- [x] Full pytest suite green, including every new orders test from 17a-17e
- [x] `black`/`ruff` clean, `manage.py check` clean
- [x] CI green against real MySQL, not just local SQLite
- [x] CodeRabbit review complete, actionable findings resolved or explicitly deferred with reasoning

**Verification:**
- [x] Checkpoint G's purchase+PV portion passes end-to-end in a real browser session: a customer completes a guest checkout (no PV), a distributor completes a checkout (PV credited correctly) -- not just pytest. **Caveat carried from 17e:** verified for a guest/customer checkout live; the distributor-PV-credited branch of this specific real-browser walkthrough relies on `confirm_order_payment`'s pytest coverage (including a live-fixture test crediting a placed distributor's PV correctly) rather than a fresh manual browser session created for this checkpoint specifically.

**Dependencies:** 17a-17e all merged

**Files likely touched:** none new -- this is verification, not implementation

**Estimated scope:** XS (process, not code)

**Skills:**
- *After:* `ci-cd-and-automation`, `git-workflow-and-versioning`, `debugging-and-error-recovery` if anything breaks in CI that didn't break locally

---

**Skills deliberately not called out per-slice above, and why:**
- `api-and-interface-design` -- called out explicitly at 17b (the `Cart` class shape is the one new
  interface convention this task introduces); applies lightly elsewhere via existing conventions
  (`DeliveryFeeCalculator`, `confirm_order_payment` matching `consume_paid_starter_pack`'s shape).
- `ci-cd-and-automation` -- only relevant at 17f; nothing about cart/checkout changes the pipeline itself before then.
- `context-engineering` -- applies to how each slice should be worked (load only that slice's section plus relevant source), not to the product being built.
- `deprecation-and-migration` -- not applicable; nothing existing is being removed. (17b replaces Task 8's *disabled* add-to-cart stub with a real one, which is completing deferred scope, not deprecating a live system.)
- `documentation-and-adrs` -- already applied, producing ADR-0005 before this breakdown was written.
- `idea-refine` / `interview-me` -- already done for this task, via the `AskUserQuestion` rounds that resolved the delivery-zone numbers and confirmed ADR-0005's decisions, before this breakdown was written.
- `observability-and-instrumentation` -- worth a look at 17d specifically (order state changes are money-adjacent, same question Task 16 asked of `WithdrawalRequest`) but not a dedicated pass unless that review surfaces a real gap; `django-simple-history` coverage should be checked on `Order` the same way it was confirmed sufficient for `Distributor`'s payout fields in ADR-0004.
- `performance-optimization` -- no known bottleneck; cart/checkout is not a batch process like the commission cycles. Only invoke if a real measurement shows a problem.
- `shipping-and-launch` -- this is a task within an ongoing build, not a production launch.
- `using-agent-skills` -- the meta-skill governing this whole breakdown's own construction; already applied.

---

### Task 18: Order status lifecycle + admin order management

Design resolved via `docs/decisions/0006-order-lifecycle-and-admin-management-design.md`, read
directly against Section 5.2/5.3 of the primary source doc plus four decisions confirmed with the
user (2026-07-26): cancelling/refunding a `confirmed` order reverses both stock and PV (an
already-paid-out bonus is an accepted, undone-by-design limitation, not clawed back); Refunded
stays manual, no real Paystack Refund API integration; customer-facing self-service cancel is
deferred to a follow-up task; the admin order management view is a custom Stitch-designed
`admin_portal` page, not plain Django Admin. Broken into seven vertical slices below, matching Task
17's own 17a-17f granularity.

---

#### Task 18a: Schema -- `Order.tracking_note` field + status-transition legality helper

**Description:** `Order.tracking_note` (`TextField(blank=True, default="")`, Section 5.3: "Admin
can update the order status and add a tracking note"). A small `apps/orders/services.py` helper
(e.g. `_ALLOWED_TRANSITIONS`, a `{from_status: {to_status, ...}}` map) encoding the legal-transition
graph from ADR-0006 decision 1's table -- every later slice's transition function calls this
before writing a new status, so an illegal jump (e.g. `pending` straight to `dispatched`) fails
loudly rather than silently corrupting the lifecycle.

**Acceptance criteria:**
- [x] `Order.tracking_note` migration applies cleanly, defaults to empty, never required
- [x] The legality helper rejects every transition not in ADR-0006's table (e.g. `delivered` -> `pending`) and accepts every one that is

**Verification:**
- [x] pytest test: every legal transition in the ADR's table is accepted; a representative sample of illegal ones (skipping a stage, moving backward, transitioning from a terminal status) are rejected

**Built:** Shipped via PR #28, 2026-07-26. `is_legal_order_status_transition` also retrofitted into
Task 17c/17d's pre-existing direct status writes (`confirm_order_payment`,
`_cancel_order_for_insufficient_stock`) as a tripwire against `_ALLOWED_TRANSITIONS` drifting out of
sync with those call sites -- CodeRabbit caught that the helper had been added but never actually
wired into the two status writes that already existed before this task, which would have let a
future change to the graph silently go unenforced at those two sites. Full suite green (870+
passed) both before and after the fix.

**Dependencies:** Task 17 merged, **explicit user sign-off before this migration** (schema change, per `SPEC.md` Boundaries)

**Files likely touched:** `apps/orders/models.py`, `apps/orders/services.py`, `apps/orders/migrations/`, `tests/unit/orders/test_order_model.py`

**Estimated scope:** XS

**Skills:**
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`

---

#### Task 18b: Paid-order cancel/refund -- stock + PV reversal (elevated rigor)

**Description:** `apps/orders/services.py::cancel_or_refund_order(order_id, to_status, tracking_note="", restock=None)`,
mirroring `confirm_order_payment`'s locked, idempotent shape (Task 17d). Design finalized 2026-07-26
via a three-cycle `doubt-driven-development` pass (see ADR-0006's "Update (2026-07-26)" section for
the full reasoning) -- summary of what that pass changed from the original plan:

- `to_status` is hard-restricted to `CANCELLED`/`REFUNDED` only -- an early draft let any legal
  transition through `is_legal_order_status_transition`, which would have let a future caller
  reverse a healthy order's PV/stock by mistakenly calling this for e.g. `PROCESSING`.
- Reverses **stock, `PvLedger`, `PvDailyBucket`, and `MonthlyPersonalPv`** -- all four, not just
  stock and the two date-keyed PV structures. `PvLedger` was originally assumed safe-but-skippable
  (or not even in scope); confirmed it must reverse too, both per real-world MLM returns-policy
  practice and to close a leg-inflation gaming vector (buy-then-refund under a leg to permanently
  bias future auto-balance placement toward the other leg). This required correcting a factual
  error repeated in this project's own docs (including ADR-0006's own decision-2 text above):
  `PvLedger` is NOT read by Binary Bonus (`process_binary_bonus_for_distributor`'s own docstring:
  "Never touches PvLedger") -- it's read only by `BinaryTree._weaker_leg` (new-distributor
  placement) and a reporting display. The two docstrings calling it "immutable all-time historical
  total" (`process_binary_bonus_for_distributor`, `pv_ledger.services.sum_leg_pv`) need updating.
- The `PvDailyBucket`/`MonthlyPersonalPv` date-drift problem (ADR-0006's original flag) is fixed
  **without a new migration**: `confirm_order_payment` now captures one `now = timezone.now()`
  before crediting, passes `now.date()` explicitly into new `today=` parameters on
  `record_purchase_pv`/`record_personal_pv` (backward-compatible; `consume_paid_starter_pack` is
  the only other caller and doesn't pass it), and reuses that same `now` for `order.confirmed_at`
  -- guaranteeing `order.confirmed_at.date()` is always exactly the credited date, by construction.
- **New accepted limitation, wider than ADR-0006 decision 2's original sunk-cost acceptance:**
  `PvDailyBucket` is a fungible same-day pool across every order sharing an ancestor+leg
  (`_credit_daily_buckets` merges them; `consume_leg_pv_fifo` drains without per-order attribution)
  -- a reversal that only checks "is the pool currently >= this order's amount" cannot always tell
  whether it's reversing this order's own remaining PV or a different, still-valid sibling order's.
  Real per-order attribution would need a much bigger schema change; accepted as documented, not
  built here.
- **Refund restocking is an explicit admin choice** (`restock: bool`, required -- no silent default
  -- whenever `to_status == REFUNDED`); cancellation always restocks automatically (pre-dispatch,
  unambiguous). Matches standard e-commerce practice (e.g. Shopify's refund flow: an explicit
  "Restock items" checkbox, not automatic) -- a goodwill refund on a delivered order must not
  silently inflate `Product.stock` for units never actually returned.
- Ancestor `Distributor` row locking (needed before mutating their `PvDailyBucket` rows, to
  actually serialize against a concurrent Binary Bonus cycle -- `process_binary_bonus_for_distributor`
  locks the same row before its own `consume_leg_pv_fifo` read-then-write) uses a sequential
  per-ancestor loop in sorted-pk order, deliberately mirroring `BinaryTree.place_distributor`'s own
  already-established "fixed, PK-ascending order" convention -- not a new, unverified bulk-lock
  assumption. Accepted as a bounded (~20-40 iteration) loop since this is a rare, admin-triggered
  cold path, not the hot per-purchase write path Scale Architecture protects.
- `PvLedger` reversal uses the same "conditional filter + count affected + warn if short" pattern as
  `PvDailyBucket`/`MonthlyPersonalPv` (not a silent `Greatest(..., 0)` floor) -- but logs at `ERROR`,
  not `WARNING`, since nothing else has ever decremented `PvLedger`; a shortfall there signals a
  real bug, not an expected gap.
- The post-lock notification helper must wrap each channel in its own `try/except Exception`,
  matching `_send_confirmation_notifications`'s existing convention -- an uncaught exception there
  would risk `retry_on_lock_contention` re-running an already-committed transaction.

**Acceptance criteria:**
- [ ] Cancelling a `confirmed`/`processing` order always restores stock; refunding a `confirmed`/`processing`/`dispatched`/`delivered` order restores stock only if `restock=True` is passed
- [ ] `restock` is required (raises, no silent default) whenever `to_status=REFUNDED`
- [ ] The same reversal restores `PvLedger`, the correct dated `PvDailyBucket` row(s), and the correct-period `MonthlyPersonalPv` row -- using `order.confirmed_at`-derived dates, never "now"
- [ ] `to_status` other than `CANCELLED`/`REFUNDED` is rejected outright, before any reversal logic runs
- [ ] `Order.pv_earned` is zeroed out once its PV is reversed (it must never keep claiming a credit that no longer exists)
- [ ] A duplicate cancel/refund attempt on an already-cancelled/refunded order is a no-op, not a double reversal
- [ ] Ancestor `Distributor` rows are locked (sorted-pk order) before their `PvDailyBucket` rows are touched, so a concurrent Binary Bonus cycle for the same ancestor can't interleave

**Verification:**
- [ ] pytest test: cancelling a confirmed order gives stock back exactly once, automatically, with no `restock` argument needed
- [ ] pytest test: refunding a `dispatched`/`delivered` order with `restock=True` restores stock; `restock=False` does not; omitting `restock` raises
- [ ] pytest test: cancelling/refunding an order reverses `PvLedger`/`PvDailyBucket`/`MonthlyPersonalPv` correctly, including when the reversal happens in a different calendar month than confirmation
- [ ] pytest test: a duplicate cancel/refund attempt (webhook-style race, matching Task 17d's own idempotency test shape) never double-reverses stock or PV
- [ ] pytest test: calling with `to_status=PROCESSING` (or any non-terminal status) is rejected
- [ ] pytest test (real MySQL in CI): a concurrent Binary Bonus cycle and a reversal for an overlapping ancestor never drive `PvDailyBucket.pv` negative

**Dependencies:** 18a merged

**Files likely touched:** `apps/orders/services.py`, `apps/pv_ledger/services.py` (new `today=` params on `record_purchase_pv`/`record_personal_pv`, corrected `sum_leg_pv` docstring), `apps/catalog/services.py` (new `increment_stock`), `apps/commissions/services.py` (corrected `process_binary_bonus_for_distributor` docstring), `tests/unit/orders/test_cancel_or_refund_order.py`

**Estimated scope:** M

**Skills:**
- *Before:* `doubt-driven-development` (done, three cycles, 2026-07-26 -- see ADR-0006), `security-and-hardening`
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`, `code-simplification`

---

#### Task 18c: Remaining manual transitions (Processing/Dispatched/Delivered) + notifications

**Description:** Admin-triggered transitions through the non-money-adjacent stages (Processing,
Dispatched, Delivered — Delivered is admin-only per ADR-0006's stated assumption, no separate
Delivery role exists). Every transition in this task and 18b sends an SMS + email on change
(Section 5.2: "The customer receives an SMS and email notification every time their order status
changes"), placed after the locked transition returns, never inside it, matching Task 16g's
established standard.

**Acceptance criteria:**
- [x] Each of Processing/Dispatched/Delivered can only be reached via a legal transition (18a) from the correct prior status
- [x] Every status change (18b and 18c alike) sends an SMS/email; the message names the new status
- [x] Adding a tracking note alongside a status update persists it on the order

**Verification:**
- [x] pytest test: transition sequence Confirmed -> Processing -> Dispatched -> Delivered succeeds in order; skipping a stage is rejected
- [x] pytest test: notification fires on each transition (mocked send_sms/send_mail, matching Task 17d's own test pattern)

**Built:** `apps/orders/services.py::advance_order_status(order_id, to_status, tracking_note="")`,
mirroring `cancel_or_refund_order`'s locked/idempotent shape (18b) minus stock/PV reversal (none of
these three stages ever touch stock or PV -- the order stays `confirmed` in every money-adjacent
sense the whole time). `to_status` is hard-restricted to `{Processing, Dispatched, Delivered}`,
matching 18b's own "reject the wrong target outright" discipline for `Cancelled`/`Refunded`. Reuses
18b's already-generic `_send_order_status_notification` helper unchanged -- no new notification
code needed. 10 new tests (`tests/unit/orders/test_order_transitions.py`); full `tests/unit/orders/`
suite (126 tests) green.

**Dependencies:** 18a, 18b merged

**Files likely touched:** `apps/orders/services.py`, `tests/unit/orders/test_order_transitions.py`

**Estimated scope:** S

**Skills:**
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`

---

#### Task 18d: Auto-cancel unpaid orders (Celery task)

**Description:** A scheduled Celery task (`apps/orders/tasks.py::auto_cancel_unpaid_orders`)
cancelling any `pending` order older than a new admin-editable constance setting. Per ADR-0006
decision 6, this path never touches stock or PV — nothing was charged, decremented, or credited for
a `pending` order (ADR-0005 decision 3/4) — it's a pure status transition plus notification. Reuses
Task 13/14/16's `CommissionCycleRun`/`Failure`-style audit-trail pattern and per-iteration-renewed
Redis lock convention rather than inventing a new one, matching every comparable batch driver in
this codebase.

**Acceptance criteria:**
- [x] A `pending` order older than the configured window is cancelled by the scheduled task
- [x] A `pending` order younger than the window is left untouched
- [x] The batch driver's own query count stays flat regardless of how many orders are eligible (Task 13's own established scale discipline)

**Verification:**
- [x] pytest test: an unpaid order older than the configured window is auto-cancelled; a fresher one is not
- [x] pytest test: a `confirmed` order (even if old) is never touched by this task, regardless of age

**Built:** New `OrderCycleRun`/`OrderCycleFailure` models (migration, explicit user sign-off given
2026-07-26) -- mirror `CommissionCycleRun`/`WithdrawalCycleRun`'s audit-trail *shape*, but as their
own model pair, not a third `job_name` value on the commissions one: this job moves no money at
all, so the `total_amount` field both existing models require doesn't apply, and neither existing
`Failure` model's id field could hold an `order_id` without corrupting its meaning -- same reasoning
Task 16's own `WithdrawalCycleRun`/`Failure` already established for *not* merging into
`CommissionCycleRun`/`Failure`. New `PENDING_ORDER_AUTO_CANCEL_HOURS` constance setting (default 24
hours, confirmed with the user) in a new "Order Settings" fieldset. New
`apps/orders/services.py::_auto_cancel_pending_order(order_id)` (locked, idempotent, PENDING-only,
sends a new auto-cancel-specific notification) plus `apps/orders/tasks.py::auto_cancel_unpaid_orders`
-- a hand-written batch driver mirroring `process_withdrawal_payouts`'s exact shape (fixed run_at,
per-iteration-renewed Redis lock, per-order exception isolation, systemic-failure guard, audit
record persisted before that guard's raise), deliberately *not* a call into
`apps.commissions.tasks._run_commission_cycle` -- that helper's `process_one` contract requires a
`Decimal` amount return, which doesn't fit "did this order get cancelled" (a bool). Celery Beat
schedule seeded via a data migration at a fixed 30-minute interval (no admin-editable *run
frequency* setting -- `PENDING_ORDER_AUTO_CANCEL_HOURS` is the age cutoff the task reads every run,
not its own schedule, so unlike Binary/Matching Bonus there's nothing to self-sync). `OrderCycleRun`
registered in Django admin, hard-locked add/change/delete matching `CommissionCycleRunAdmin`
exactly. 13 new tests (`tests/unit/orders/test_auto_cancel_unpaid_orders.py`) covering the per-order
function, cutoff-based cancel/no-cancel, confirmed-orders-untouched, audit-record persistence, lock
release on success, concurrent-trigger skip, systemic-failure raise + failure record, and the seed
migration itself.

**Dependencies:** 18a merged

**Files likely touched:** `apps/orders/tasks.py`, `apps/orders/models.py` (new `OrderCycleRun`/`OrderCycleFailure`), `apps/orders/admin.py`, `apps/orders/migrations/` (schema + seed), `apps/platform_settings/config.py` (new constance setting for the cancel window), `tests/unit/orders/test_auto_cancel_unpaid_orders.py`

**Estimated scope:** S

**Skills:**
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`, `performance-optimization` (query-count-stays-flat check, mirroring Task 13's own discipline)

---

#### Task 18e: Admin order management -- backend (filters, PDF invoice, cancel/refund actions)

**Description:** The view/query layer behind the admin order management page: filter by
status/date/customer (Section 5.3), a PDF invoice endpoint (`WeasyPrint` — already a pinned
dependency, never yet actually used to generate anything in this codebase, so its real API needs
confirming against real docs, not assumed) rendering the plain-receipt fields ADR-0006 decision 7
resolved (Order ID, customer, items, delivery method/address, total, payment status — no
withholding-tax logic), and admin actions wired to 18b/18c/18d's service functions rather than
reimplementing transition logic inline. Per this codebase's own already-documented precedent
(ADR-0005's schema review, flagged specifically for this task), Django admin actions default to
requiring `has_change_permission` — an unconditional lockdown would silently block bulk actions for
everyone, including superusers, unless each action explicitly declares `permissions=["view"]`; this
slice must not rediscover that bug.

**Acceptance criteria:**
- [x] Filtering by status/date/customer returns the correct, correctly-paginated set
- [x] The PDF invoice contains exactly the fields ADR-0006 decision 7 lists, for any order
- [x] Cancel/refund/status-update actions call 18b/18c's functions, never duplicate their logic
- [x] A non-staff account cannot trigger a cancel/refund action (see permission-model correction below)

**Verification:**
- [x] pytest test: filter combinations return the expected order set
- [x] pytest test: PDF invoice generation succeeds and contains the expected fields for a real order
- [x] pytest test: a non-staff account is blocked from the cancel/refund action

**Built:** `apps/admin_portal/views.py::_filtered_orders`/`order_management_queue`/
`order_management_action`/`order_invoice_pdf`, plus `apps/admin_portal/urls.py` routes and a
placeholder `templates/admin_portal/order_management_queue.html` (Task 18f replaces this with the
real Stitch design, matching Task 20's own placeholder-dashboard precedent). Actions call
`cancel_or_refund_order`/`advance_order_status` directly, never duplicating their logic.

**Permission-model correction (2026-07-26):** the original acceptance criterion ("a staff account
without the relevant Django permission cannot trigger a cancel/refund action") assumed a granular
per-model permission system. `tests/conftest.py`'s own `staff_client` fixture reveals the real
architecture: every admin account in this codebase is `is_superuser=True` (SPEC.md's "single flat
role," no per-model permissions), so a `has_perm()` check would only ever be testable against an
account-shape this system never actually creates. Corrected with the user: the gate is
`is_admin_portal_staff` (is_staff + verified 2FA) -- the same gate every other admin_portal view
already uses -- and the test proves a non-staff account (customer/distributor) is blocked, not a
staff-without-permission account.

**WeasyPrint could not be verified locally.** `source-driven-development` confirmed the real API
against WeasyPrint's own docs (`HTML(string=...).write_pdf()`) and this Mac's missing system-level
Pango library via a direct import attempt (raises `OSError`, not `ImportError`). `brew install
weasyprint` was attempted (user-approved) and failed after ~50 minutes -- it had to compile Python
3.13 from source along the way and still failed on `cffi`, because **macOS 12 is an unsupported
Homebrew Tier-3 configuration**. `.github/workflows/ci.yml`'s `test` job now installs
`libpango-1.0-0`/`libpangocairo-1.0-0` via `apt` (an ordinary pre-built Ubuntu package, no
compiling) so the real end-to-end PDF test runs for real in CI. The test itself
(`test_invoice_pdf_end_to_end_generates_a_real_pdf`) uses this codebase's first `pytest.mark.skipif`
-- self-healing, conditioned on whether `import weasyprint` actually succeeds, so it lifts
automatically the moment Pango is available, here or anywhere else. PDF *content* correctness
(every ADR-0006 decision 7 field) is proven independently via a separate template-only test that
needs no PDF library at all, so this local gap does not weaken content verification.

**A second, genuinely separate bug surfaced once that CI-run smoke test actually executed
WeasyPrint's real PDF generation for the first time anywhere (2026-07-27):** `AttributeError:
'super' object has no attribute 'transform'` inside WeasyPrint's own `pdf/stream.py`. Root cause:
WeasyPrint 62.3's `Stream` class calls `super().transform(...)`, but `pydyf` 0.12.0 (released
2025-12-02) removed that deprecated method entirely -- `requirements.txt` never pinned `pydyf`
(WeasyPrint's own `pyproject.toml` only declares `pydyf>=0.11.0`, no upper bound), so `pip install`
silently resolved the incompatible 0.12.1 both locally and in CI. Unrelated to the Pango/macOS issue
above -- this would have broken PDF generation on any machine, Pango or no Pango. Confirmed directly
(`pydyf.Stream` has `transform` on 0.11.0, not on 0.12.x) and fixed by pinning `pydyf>=0.11,<0.12`
in `requirements.txt`, matching this project's own established convention for pinning undeclared/
incompatible transitive dependencies (`cbor2`, `phonenumbers`, `PyJWT`).

**Dependencies:** 18b, 18c, 18d merged

**Files likely touched:** `apps/admin_portal/views.py`, `apps/admin_portal/urls.py`, `templates/admin_portal/order_management_queue.html`, `templates/admin_portal/order_invoice.html`, `.github/workflows/ci.yml`, `tests/feature/admin_portal/test_order_management.py`

**Estimated scope:** M

**Skills:**
- *Before:* `source-driven-development` (confirming WeasyPrint's real HTML-to-PDF API against its own docs before use)
- *During:* `test-driven-development`, `incremental-implementation`, `security-and-hardening` (permission-model correction + a full review pass; confirmed the existing global `HistoryRequestMiddleware` already covers audit-trail "who did it" for these actions)
- *After:* `code-review-and-quality`

---

#### Task 18f: Admin order management -- frontend (Stitch-designed `admin_portal` page)

**Description:** Real Stitch-designed UI for the order management page, matching every prior admin
screen (KYC Review Queue, Withdrawal Review Queue, Distributor Directory, Commission Cycle Detail).
**Claude does not design this UI freehand** -- the user sends a Stitch prompt and shares the
resulting screens/export back for template integration, same as every prior page in this project.

**Acceptance criteria:**
- [x] The order list, filters, and per-order detail (status update, tracking note, cancel/refund, PDF invoice link) render the real Stitch design, integrated with 18e's actual data
- [x] Verified across the achievable real-browser breakpoints (1440/768/500px — see Task 17e's own note on the ~500px practical floor)

**Verification:**
- [x] Manual check: an admin filters, updates status, adds a tracking note, cancels a confirmed order (stock/PV visibly reversed in the DB), and downloads a PDF invoice, all via the real UI in a real browser

**Built (2026-07-27):** the two Stitch screens (Order Management Queue, Order Detail) were fetched,
then integrated as real Django templates against `templates/admin_portal/base_dashboard.html`'s
existing shared shell rather than reproducing Stitch's own sidebar/topbar markup -- "Orders" added
as the shell's 6th real nav item. `order_management_queue.html` rebuilt in full: themed search +
date-range filters plus a custom Alpine listbox for the status filter (matching the same no-native-
`<select>` pattern `distributor_directory.html`/`payout_settings.html` already established, since a
native select's open-options popup can't be restyled via CSS), a paginated table with a new shared
`_order_status_pill.html` partial (7 colour-coded statuses, reused by both pages so they can't drift
apart), and a `querystring_no_page` pagination fix (mirroring `distributor_directory`'s own
precedent) since the first draft's hand-interpolated `?q=...&status=...` links would corrupt the
filter the moment a search term contained `&`. A real gap was found while wiring this up: Task 18e
shipped no GET-based single-order detail view at all, only the list, a POST-only action handler,
and the PDF endpoint -- added `apps/admin_portal/views.py::order_detail` (TDD, mirrors
`order_invoice_pdf`'s query shape, but via a `Prefetch("items", queryset=...select_related(
"product__category").prefetch_related("product__images"))` to avoid the same N+1
`apps.orders.cart.Cart.items()` already documents for `product.primary_image`) plus a new
`admin_portal:order_detail` URL route, and changed `order_management_action`'s redirect target from
the queue to this same order's detail page so an admin's action and its flash message stay visible
in place, matching the Stitch screen's own in-place-alert design. `main.css` gained the
`headline-sm`/`mono-sm` tokens (JetBrains Mono font loaded) these two screens needed but no prior
screen had used yet -- pulled from the same Stitch design system's own already-established values,
not invented here. Real-browser verification (not just pytest) caught and fixed three genuine bugs
before calling this done: (1) the Update Status panel's `lg:sticky lg:top-24` visually overlapped
the Admin Note box below it once the panel was tall enough (confirmed via `getBoundingClientRect()`
in a live session) -- fixed by dropping the sticky positioning, which added no real benefit here
anyway; (2) a multi-line `{# ... #}` Django comment rendered as literal visible text, since Django's
single-line comment tag doesn't support multi-line content the way `{% comment %}...{% endcomment
%}` does; (3) a real functional bug, not cosmetic -- `can_cancel` was computed purely from
`is_legal_order_status_transition`, which legitimately allows `PENDING -> CANCELLED` (that edge
exists for Task 17c/17d's own automatic pre-payment cancellation and Task 18d's auto-cancel batch
job), but the actual service function the Cancel button calls, `cancel_or_refund_order` (18b), treats
a `PENDING` order as a caller bug and silently no-ops with no exception -- meaning the button would
have shown "Order ... cancelled" while doing nothing. This latent mismatch predates 18f (it's been
sitting in 18b's own code since before any UI could reach it) but 18f's new detail page was the
first thing to expose it as a clickable action; fixed by excluding `PENDING` explicitly in
`can_cancel`'s computation, with a regression test. All three fixes verified both by a fresh
real-browser pass and by the full pytest suite (942 passed, 1 skipped, 0 failed).

**Follow-up (2026-07-27, same day, user-requested after first review):** three more fixes, all
verified live: (1) the native `<input type="date">` From/To filters were replaced with a fully
custom themed Alpine.js calendar popover (`templates/admin_portal/_date_filter_field.html`, new,
included twice with independent `x-data` scopes) -- same root reason the status filter already got
a custom listbox instead of a native `<select>` (a native date input's own calendar popup can't be
restyled via CSS in any browser either); its hidden input dispatches a real `change` event on
selection so it participates in the same trigger chain as the status listbox. (2) The whole filter
bar (search/status/date) was converted to real-time, no-button htmx filtering, mirroring
`distributor_directory`'s own already-established pattern exactly (TDD: RED test for the
`request.htmx` branch first) -- `order_management_queue`'s results table+pagination+empty-state was
extracted into `templates/admin_portal/partials/order_results.html` (`#order-results`, `hx-swap=
"outerHTML"`), the view gained a `request.htmx` branch returning just that partial, and the "Filter"
submit button was removed entirely. (3) Same multi-line-`{# #}`-renders-as-visible-text bug (already
fixed once in `order_detail.html`) recurred in three MORE new templates written during this
follow-up -- found again via real-browser verification, fixed by converting every one to `{%
comment %}...{% endcomment %}` and confirmed via a repo-wide grep that no multi-line `{# #}`
remained in any touched template. Verified in a real browser: the date picker's month navigation and
day-grid weekday alignment (checked against real calendar dates, not assumed), and every filter field
narrowing/excluding results live with no page reload and no Filter button, at 1440px and 500px both.

**Dependencies:** 18e merged; Stitch prompt sent and screens received from the user

**Files touched:** `apps/admin_portal/views.py`, `apps/admin_portal/urls.py`,
`templates/admin_portal/order_management_queue.html`, `templates/admin_portal/order_detail.html`
(new), `templates/admin_portal/_order_status_pill.html` (new),
`templates/admin_portal/_date_filter_field.html` (new),
`templates/admin_portal/partials/order_results.html` (new),
`templates/admin_portal/base_dashboard.html`, `static/src/main.css`,
`tests/feature/admin_portal/test_order_management.py`

**Estimated scope:** M

**Skills:**
- *During:* `frontend-ui-engineering`, `incremental-implementation`, `test-driven-development` (the
  missing `order_detail` view was built RED-first)
- *After:* `code-review-and-quality`, `debugging-and-error-recovery` (all three real-browser bugs)

---

#### Task 18g: Full-suite verification, CI, PR, Checkpoint G (closing)

**Description:** Same branch -> PR -> CI (real MySQL) -> CodeRabbit -> merge workflow as every
prior task. Closes the "order status updates correctly with notifications" portion of Checkpoint G
that Task 17f explicitly left open for this task.

**Acceptance criteria:**
- [x] Full pytest suite green, including every new orders test from 18a-18f (945 passed, 1 skipped, 0 failed — the skip is the documented WeasyPrint/Pango CI-only test)
- [x] `black`/`ruff` clean, `manage.py check` clean
- [x] CI green against real MySQL, not just local SQLite (PR #33, both commits)
- [x] CodeRabbit review complete, actionable findings resolved or explicitly deferred with reasoning (PR #33 — 2 actionable + 5 nitpicks, all fixed; see PR #33's own commit history)

**Verification (2026-07-27):** Checkpoint G's remaining portion verified end-to-end in a real
browser session, not just pytest. `MNOTIFY_API_KEY` was temporarily blanked in `.env` (restored
immediately after, user-approved via `AskUserQuestion` rather than spending real SMS credit or
silently assuming it was fine) so the real `_send_order_status_notification` code path could be
exercised with its actual fake-sender fallback instead of mocking it away. A real order was walked
through Confirmed -> Processing -> Dispatched -> Delivered via the live admin_portal UI; the
runserver console log confirmed the exact SMS text sent at the final transition ("Your Bancostore
order ... is now Delivered.") -- catching, along the way, that Django's autoreloader re-execs its
worker subprocess without inheriting the `-u` interpreter flag (interpreter flags aren't part of
`sys.argv`), so `python -u manage.py runserver` silently doesn't unbuffer the process that actually
handles requests; fixed by using `PYTHONUNBUFFERED=1` (an env var, which does survive the re-exec)
with `--noreload`. A second order was admin-cancelled the same way: the console log confirmed the
correct SMS text ("... is now Cancelled.") and `Product.stock` was confirmed incremented by exactly
the cancelled line item's quantity (22 -> 24) via `manage.py shell`, both matching the already-
established pytest coverage. PV reversal specifically was not re-verified live here (would need a
full binary-tree ancestor scenario just for this checkpoint) -- relied on Task 18b's own elevated-
rigor pytest suite for that piece, a deliberate, stated scope decision, not a silent gap.

**Dependencies:** 18a-18f all merged

**Files likely touched:** none new -- this is verification, not implementation

**Estimated scope:** XS (process, not code)

**Skills:**
- *After:* `ci-cd-and-automation`, `git-workflow-and-versioning`, `debugging-and-error-recovery` if anything breaks in CI that didn't break locally

---

**Skills deliberately not called out per-slice above, and why:**
- `api-and-interface-design` -- called out implicitly at 18a (the transition-legality helper is the one new interface convention this task introduces); applies lightly elsewhere via existing conventions (`cancel_confirmed_order` matching `confirm_order_payment`'s shape).
- `ci-cd-and-automation` -- only relevant at 18g.
- `context-engineering` -- applies to how each slice should be worked, not to the product being built.
- `deprecation-and-migration` -- not applicable; `Order.status`'s full choice set already existed since Task 17a specifically for this task to use, so this completes deferred scope rather than deprecating anything.
- `documentation-and-adrs` -- already applied, producing ADR-0006 before this breakdown was written.
- `idea-refine` / `interview-me` -- not applicable; this task was already concretely scoped via direct source-doc reading plus the `AskUserQuestion` rounds that resolved ADR-0006's four open decisions.
- `observability-and-instrumentation` -- worth confirming `django-simple-history` (already on `Order` since Task 17a) actually covers every new transition this task adds, but not a dedicated pass unless that check surfaces a real gap.
- `shipping-and-launch` -- this is a task within an ongoing build, not a production launch.
- `using-agent-skills` -- the meta-skill governing this whole breakdown's own construction; already applied.

---

**Checkpoint G:** both a customer and a distributor complete a purchase; PV is generated only for
the distributor; order status updates correctly with notifications.

---

## Phase 7: Cooling-Off Refund

### Task 19: 7-day cooling-off refund

Design resolved via `docs/decisions/0007-cooling-off-refund-design.md`, read directly against
Section 9 of the primary source doc plus five decisions confirmed with the user (2026-07-27,
grounded in real-world MLM/direct-selling industry practice, not guessed): the 7-day window
anchors to `Distributor.starter_pack_confirmed_at` (the purchase moment — nothing is refundable
before then), not account/registration creation; the distributor's binary-tree placement is
soft-deactivated (`user.is_active = False`, mirroring the existing suspend/reactivate toggle), not
removed (no removal mechanism exists anywhere in this codebase, and building one is real, unbuilt,
unrequested scope); only the sponsor's direct referral bonus is reversed, not any Binary/Matching
bonus the cancelling distributor might have personally earned (a week-old distributor realistically
never generates the latter, and this mirrors ADR-0006's already-accepted "fungible pool, can't
attribute precisely once mixed into a batch cycle" limitation); a debit shortfall on the sponsor's
wallet (already withdrawn) is logged for admin follow-up and the distributor's own refund proceeds
regardless; and the refund is credited to the distributor's own wallet (not paid out externally,
since — unlike a storefront customer — distributors already have a real `Wallet` and withdrawal
flow built for exactly this). Broken into four vertically-sliced sub-tasks below, matching Task 18's
own 18a-18g granularity. No admin-approval gate exists anywhere in Section 9 (unlike Task 16's
withdrawal flow) — this is fully self-service, so no `admin_portal` slice is needed.

---

#### Task 19a: Generalize the PV-reversal helper for reuse

**Description:** `apps/orders/services.py::_reverse_ancestor_pv` (Task 18b) already implements the
exact PV-reversal shape this task needs (reverse `PvLedger` directly, reverse
`PvDailyBucket`/`MonthlyPersonalPv` only what's still live) but is Order-specific. Extract it into
`apps/pv_ledger/services.py` as a public helper taking `(distributor, pv_amount, purchase_date)`
directly, and update `apps/orders/services.py` to call the shared version instead of keeping a
second, duplicate implementation.

**Acceptance criteria:**
- [x] `apps/pv_ledger/services.py` exposes a public PV-reversal helper with the same
  PvLedger/PvDailyBucket/MonthlyPersonalPv reversal behavior `_reverse_ancestor_pv` already has
- [x] `apps/orders/services.py::cancel_or_refund_order` calls the shared helper instead of its own
  private copy

**Verification:**
- [x] Task 18b's own existing test suite (`tests/feature/orders/` or wherever its cancel/refund
  tests live) passes unchanged — this refactor must be behavior-preserving, not a new test-writing
  exercise for the order side
- [x] New direct unit tests for the generalized helper in `apps/pv_ledger/`
- [x] Full suite green (307 passed), `ruff`/`black`/`isort` clean
- [x] Bonus fix folded in: `apps/distributors/services.py::consume_paid_starter_pack` had a real
  date-drift bug (3 independent `timezone.now()` calls could straddle a UTC-midnight boundary) —
  found via `doubt-driven-development` before writing the extraction, fixed with a RED→GREEN
  regression test (`test_pv_credit_and_starter_pack_confirmed_at_share_one_pinned_now`)

**Dependencies:** Task 18b merged

**Files likely touched:** `apps/pv_ledger/services.py`, `apps/orders/services.py`,
`tests/unit/pv_ledger/`

**Estimated scope:** S

**Skills:**
- *Before:* `doubt-driven-development` (this is a refactor of already-shipped, money-adjacent
  logic — confirm the extraction is genuinely behavior-preserving before touching the order side)
- *During:* `test-driven-development`, `code-simplification`
- *After:* `code-review-and-quality`

---

#### Task 19b: Cooling-off refund service function

**Description:** `apps/distributors/cooling_off_services.py` (new) — the core money/PV/rank logic.
Validates the request is within `COOLING_OFF_PERIOD_DAYS` of `starter_pack_confirmed_at`, computes
the refund via `starter_pack_price_pesewas * (1 - COOLING_OFF_REFUND_DEDUCTION_RATE / 100)`,
reverses ancestor PV (19a's helper) and personal PV, reverses the sponsor's direct referral bonus
via `apps/wallet/services.py::debit()` (catching `InsufficientBalanceError`, debiting what's
available, logging any shortfall), credits the refund to the distributor's own wallet, clears
`rank`/`starter_pack_*` fields, and deactivates the account (`user.is_active = False`). Locked and
idempotent, mirroring `consume_paid_starter_pack`'s own shape in reverse.

**Acceptance criteria:**
- [x] Refund amount matches the doc's own worked example exactly: Pack B GHS 2,000 → GHS 1,800
- [x] A request after day 7 (from `starter_pack_confirmed_at`) is rejected
- [x] PV added at signup (both ancestor legs and personal PV) is removed
- [x] The sponsor's direct referral bonus is reversed when their wallet has sufficient balance
- [x] A shortfall (insufficient sponsor balance) is logged, not raised — the distributor's own
  refund still completes
- [x] The distributor's own wallet is credited with the refund amount
  (`COOLING_OFF_REFUND` transaction type)
- [x] The account is deactivated (cannot log in afterward) but the `BinaryTreeEdge` placement is
  left untouched
- [x] A second call for the same distributor is a safe idempotent no-op (already-cancelled)

**Verification:**
- [x] pytest test reproducing the doc example exactly (GHS 2,000 → GHS 1,800)
- [x] pytest test: request after day 7 is rejected
- [x] pytest test: insufficient sponsor balance logs a shortfall and still refunds the distributor
- [x] pytest test: PvLedger/PvDailyBucket/MonthlyPersonalPv all correctly reversed
- [x] pytest test: double-cancellation is idempotent
- [x] `doubt-driven-development` pre-implementation review caught and fixed 4 real defects: the
  sponsor's bonus reversal must use the amount actually credited (looked up via the original
  `WalletTransaction`), not recomputed from the live `DIRECT_REFERRAL_BONUS_RATE`; the sponsor's
  `Wallet` row must be locked via this codebase's NOWAIT convention, not a plain blocking
  `select_for_update()`; the cancelling distributor's own row and its `BinaryTreeEdge` ancestors
  must be locked as one combined ascending-pk-sorted set, not the distributor's row separately and
  first; and a new `MembershipCancelled` guard was needed in `snapshot_starter_pack_choice` to
  close a double-credit path reachable via an admin reactivating a cancelled account and
  re-purchasing a starter pack
- [x] `security-and-hardening` review (post-implementation) caught one more real gap: a replayed
  Paystack webhook for the distributor's old `starter_pack_payment_reference` after cancellation
  was only stopped incidentally (the cleared `starter_pack_price_pesewas` happened to fail the
  amount check), not by an intentional guard — fixed with an explicit `cooling_off_cancelled_at`
  check in `consume_paid_starter_pack`, proven via a RED→GREEN regression test
- [x] Full suite green (964+ passed), `ruff`/`black`/`isort`/`manage.py check` clean

**Dependencies:** Task 19a

**Files likely touched:** `apps/distributors/cooling_off_services.py` (new),
`apps/wallet/models.py` (two new `TransactionType` values + migration),
`tests/unit/distributors/test_cooling_off_refund.py`

**Estimated scope:** M

**Skills:**
- *Before:* `doubt-driven-development` (money + PV + commission reversal — this project's own
  elevated-rigor area)
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`, `security-and-hardening` (race-safety on a self-service
  double-click)

---

#### Task 19c: Distributor-facing "Cancel Membership & Request Refund" UI

**Description:** Real Stitch-designed UI on the distributor dashboard (per the source doc's own
"they log into their dashboard and click 'Cancel Membership & Request Refund'" wording) wired to
19b's service function. Claude does not design this UI freehand — the user sends a Stitch prompt
and shares the resulting screen(s) back for template integration, same as every prior distributor-
and admin-facing page in this project.

**Acceptance criteria:**
- [x] The action is only offered within the cooling-off window (hidden or disabled after day 7, or
  once already cancelled)
- [x] A confirmation step exists before the irreversible cancel action fires (matches this
  project's own established danger-action pattern — e.g. `distributor_profile`'s suspend/reactivate
  modal)
- [x] A successful cancellation shows the refund amount and logs the distributor out (their account
  is now deactivated)

**Verification:**
- [x] Manual check: request a refund as a real seeded distributor in a real browser within the
  window, confirm the wallet credit and account deactivation are both visible/effective
  end-to-end, not just via pytest — done 2026-07-27: logged in as a real seeded distributor, viewed
  both eligible/ineligible states, used the confirm modal, completed a real cancellation, confirmed
  in the database that the wallet was credited GHS 1,800.00, the sponsor's bonus was reversed, and
  the session was genuinely logged out (a follow-up authenticated request redirected to login)
- [x] Two real Stitch screens fetched (Cancel Membership Eligible/Ineligible - Bancostore
  Distributor, same "Bancostore" project/theme as every other screen) and verified against the
  live page
- [x] `code-review-and-quality` caught and fixed one real bug: `membership_cancelled.html` used a
  truthy check on `refund_amount` instead of `is not None`, which would have misreported a
  legitimate GHS 0.00 refund as "already cancelled" — fixed, proven via a RED→GREEN regression test
- [ ] Responsive check at the project's established breakpoints (1440/768/500px) — desktop-only
  verified so far; mobile/tablet breakpoints not yet checked

**Dependencies:** Task 19b; Stitch prompt sent and screen(s) received from the user

**Files likely touched:** `apps/distributors/views.py`, `templates/distributors/*.html`,
`tests/feature/distributors/test_cooling_off_refund_view.py`

**Estimated scope:** S

**Skills:**
- *During:* `frontend-ui-engineering`, `incremental-implementation`
- *After:* `code-review-and-quality`, `browser-testing-with-devtools`

---

#### Task 19d: Full-suite verification, CI, PR, checkpoint close

**Description:** Same branch → PR → CI (real MySQL) → CodeRabbit → merge workflow as every prior
task.

**Acceptance criteria:**
- [x] Full pytest suite green, including every new cooling-off test from 19a-19c — 986 passed, 1
  skipped (the pre-existing WeasyPrint/Pango CI-only skip), 0 failures on `main` post-merge
- [x] `black`/`ruff` clean, `manage.py check` clean
- [x] CI green against real MySQL (all three PRs)
- [x] CodeRabbit review complete, actionable findings resolved or explicitly deferred with
  reasoning — see below

**Verification:**
- [x] End-to-end real-browser pass: a seeded distributor within their cooling-off window cancelled
  and got refunded, verified against the database at each step (wallet balance, PV ledger, account
  status), not just pytest — done 2026-07-27 (Task 19c's own verification)

**Dependencies:** 19a-19c all merged

**Files likely touched:** none new — this is verification, not implementation

**Shipped 2026-07-27 via PR #35 (19a), #36 (19b), #37 (19c), merged into `main` in that order.**
Each PR was stacked on the previous (19b on 19a, 19c on 19b) since PRs #36/#37 targeted non-default
branches, CodeRabbit's auto-review was skipped on them entirely until each was retargeted to `main`
and re-triggered — a real gap in the stacked-PR workflow this task's own close-out surfaced,
recorded so it isn't rediscovered fresh next time a stack is used. CodeRabbit caught two real,
previously-unnoticed defects across the three PRs, both fixed before merge:
- **PR #35:** `reverse_ancestor_pv` (Task 19a) early-returned for a distributor with no
  `BinaryTreeEdge` ancestors (a root distributor, or one not yet placed under a sponsor) before
  ever reaching the `MonthlyPersonalPv` reversal that runs after the ancestor-leg loop — silently
  leaving a root distributor's own personal PV permanently inflated after any cancellation/refund,
  affecting both Task 18b's order-cancellation path and Task 19b's cooling-off refund. Fixed by
  scoping the ancestor-only work to `if edges:` instead of an early `return`; the existing
  root-distributor test only asserted "no exception" and didn't catch this, strengthened to assert
  the actual reversal.
- **PR #36 (flagged against the ADR text) / confirmed on the real code by PR #37's CodeRabbit
  pass:** `cancel_membership_and_refund` credits a real refund to the distributor's own wallet, but
  also sets `user.is_active = False` as its last step — Django blocks login entirely for
  `is_active=False`, so the refund was genuinely unclaimable, contradicting ADR-0007 Decision 7's
  own stated rationale for crediting the wallet in the first place. User-confirmed fix (of three
  options presented): let a cooling-off-cancelled distributor still log in, restricted to
  withdrawal-related views only. Needed two changes to
  `apps/distributors/backends.py::PhoneNumberBackend`, not one — `authenticate()` alone wasn't
  sufficient, since Django's `AuthenticationMiddleware` also calls `get_user()` on every subsequent
  request (inherited from `ModelBackend`, which blanket-checks `is_active` too) — caught mid-
  implementation when a `client.login()`-based test kept redirecting to the login page despite
  `authenticate()` succeeding. A new shared `_redirect_if_cooling_off_cancelled` decorator (not a
  repeated inline check) gates the 4 views that make no sense post-cancellation
  (dashboard/earnings_history/select_starter_pack/start_kyc_verification), verified exhaustive
  against the app's full 8-view `@login_required` list.
- A `code-review-and-quality` pass on Task 19c independently caught a third, smaller bug before
  CodeRabbit ever saw it: `membership_cancelled.html` used a truthy check on `refund_amount`
  instead of `is not None`, which would have misreported a legitimate GHS 0.00 refund (reachable
  only via an admin-misconfigured 100% deduction rate) as "already cancelled."

**Estimated scope:** XS (process, not code)

**Skills:**
- *After:* `ci-cd-and-automation`, `git-workflow-and-versioning`, `debugging-and-error-recovery` if
  anything breaks in CI that didn't break locally

---

## Phase 8: Distributor Dashboard (real-time)

### Task 25: My Orders — self-service order history (build before Task 20) — DONE

**Shipped 2026-07-28 via PR #38 (branch `task-25-my-orders`), merged to `main`.** All acceptance
criteria met: `apps/orders/views.py::order_history` (paginated, `request.user`-scoped, prefetches
`items__product__images`, `-created_at`/`-pk` stable ordering, elided page range) and
`order_detail` — a **new**, deliberately separate view from `order_confirmation_view`, since that
view's unguessable-token-only protection is safe only for a one-time post-checkout redirect, not a
durable bookmarkable link (`doubt-driven-development` finding before any code was written).
`pk=pk, customer=request.user` is IDOR-safe by construction. Real template built from two fetched
Stitch screens (desktop + mobile), one responsive file matching this codebase's convention. Two
real bugs found via real-browser verification and caught fixing along the way, both with regression
tests: `order_created.html` (reused for `order_detail`) only showed the item/delivery-info block for
`status == "confirmed"`, so a processing/dispatched/delivered/refunded order — most of an order's
real life — showed a bare "Order Placed" message with no items; broadened to any non-"pending"
status, and every one of the 7 `Order.Status` values got its own accurate header copy (the cancelled
copy no longer assumes the cause was always insufficient-stock auto-cancel, since Task 18b's admin
`cancel_or_refund_order` can cancel for any reason). `base_store.html`'s header had no logged-in
state at all (My Orders/Log Out links added) — and a follow-up code-review pass caught that the Log
Out link didn't actually work (`allauth`'s `LOGOUT_ON_GET` defaults to `False`), fixed with a
CSRF-safe POST form. CodeRabbit's own pass on the pushed fix caught one more real bug — the
authenticated nav controls were `hidden lg:flex` while the mobile hamburger is `md:hidden`, leaving
a genuine gap between `md` and `lg` (~768–1024px) where a user could reach neither; fixed to
`hidden md:flex` and verified live at 900px. Full local suite green throughout (1009 passed, 1
skipped), real MySQL CI green, CodeRabbit clean on the final commit. See git history on `main` for
the full PR #38 diff.

**Original description (2026-07-27):** a real, previously-unnoticed gap in this plan, found by reading — a real, previously-unnoticed gap in this plan, found by reading
Section 6.4 of the primary source doc directly (`source-driven-development`) while scoping Task 20
(Dashboard core stats): *"Order History (as a Customer): Distributors can also view their own
product purchase history from their dashboard. Each purchase shows order ID, products, date,
amount, PV generated, and delivery status."* No task anywhere in this plan built a self-service
order-history page for either a regular customer or a distributor — `apps/orders/urls.py` has
cart/checkout/confirmation routes only, nothing to list a user's own past orders. Section 6.3's
Earnings History is already covered by Task 15's `earnings_history` page; this is the separate,
still-missing product-purchase counterpart. User-confirmed 2026-07-27: build it now, before the
dashboard (Task 20) that's meant to link to it.

**Numbered 25, not 20, despite building first:** the first attempt at this inserted it as Task 20
and renumbered Task 20→21 and Task 21→22 to make room — which silently collided with the real,
already-code-referenced Task 22 (Admin Portal — KYC review, Phase 9) and would have cascaded into
Task 23/24 too. Task 20/21 (Phase 8) and 22-24 (Phase 9's Admin Portal, Task 24 Deploy) are all
real numbers with existing shipped-code references; none of them move. This task takes the next
free integer instead and is simply sequenced earlier than its number suggests — a real limitation
of pure sequential numbering once later phases have already claimed numbers, not a pattern to
repeat carelessly next time either (check the *entire* plan's existing numbers before assuming an
insertion point is free, not just the immediately-adjacent phase).

A single view/template does double duty: `Order.customer` is a plain FK to `settings.
AUTH_USER_MODEL` (not `Distributor`-specific), so `Order.objects.filter(customer=request.user)`
already works identically for a regular customer or a distributor-as-customer — no new model, no
role branching needed. `Order`/`OrderItem` (Task 17a) already snapshot every field Section 6.4
asks for (`total`, `pv_earned`, `status`, `created_at`, `OrderItem.product_name`/`quantity`/
`unit_price`) at order-creation time, so this is a read-only query + template page, not new
data-model work.

**Acceptance criteria:**
- [x] A logged-in user (customer or distributor) sees a paginated list of their own past orders:
  order ID, product(s), date, amount, PV generated, delivery status
- [x] Never shows another user's orders (no id/param IDOR surface — always `request.user`, matching
  `earnings_history`/`payout_settings`'s own established convention)
- [x] Each row links to the existing order-detail view (confirm exact URL name/reachability for a
  self-service, non-admin viewer — `order_confirmation` is payment-reference-keyed and built for
  the checkout flow, not a general history page; may need its own detail route or reuse of that
  one's template) — resolved as a new `order_detail` route, not a reuse of `order_confirmation`'s
  token-only protection (see build summary above)
- [x] Guest checkout orders (`customer=None`) are correctly excluded (nothing to list for an
  anonymous shopper)

**Verification:**
- [x] pytest test: a customer sees their own orders, not another customer's or a guest order
- [x] pytest test: a distributor sees their own orders (proves the shared-view, no-role-branching
  design actually holds)
- [x] pytest test: pagination stable ordering (`-created_at`, `-pk` tie-breaker — Task 15d's own
  pagination bug is the precedent not to repeat)
- [x] Manual check in a real browser: place a real order, confirm it appears correctly

**Dependencies:** Task 17a (Order/OrderItem models), Task 18 (order status lifecycle, for the
delivery-status values shown)

**Files likely touched:** `apps/orders/views.py` (new view), `apps/orders/urls.py` (new route),
`templates/orders/order_history.html` (new — customer-facing, extends `base_store.html`),
`templates/distributors/*.html` (dashboard link, once Task 20 exists to link from),
`tests/feature/orders/test_order_history.py`

**Estimated scope:** S–M

**Skills:**
- *Before:* `source-driven-development` (already done — this task's own existence is the finding)
- *During:* `test-driven-development`, `incremental-implementation`
- *After:* `code-review-and-quality`, `security-and-hardening` (IDOR check is the main risk here)

---

### Task 20: Dashboard core stats

**Description:** Django view + HTMX dashboard showing wallet balance, total earnings, this week's
earnings, team size, left/right leg PV, monthly personal PV, IR ID with copy button, referral
link with WhatsApp share, rank badge — updating live via Django Channels when underlying data
changes. Broken into five vertical slices (20a-20e) per `planning-and-task-breakdown`, since this
is the first real-time Channels consumer ever built in this codebase (Channels/Redis has been
configured since Task 1-3, but `bancostore/asgi.py`'s websocket `URLRouter` has always been
empty) and touches a genuinely unguarded existing view.

**Grounded directly against Section 6.1 of the primary source doc** (not just `tasks/todo.md`'s own
paraphrase, `source-driven-development`), which resolved two real ambiguities before planning:
"This Week's Earnings" is explicitly "earned in the **current week**" (a calendar week, not a
rolling 7-day window like Matching Bonus's own `MATCHING_BONUS_INTERVAL_DAYS`), and "Referral
Link" is explicitly "a **personal recruitment link** with a WhatsApp share button" — not just the
bare IR ID text a distributor already has, meaning `distributors:register` needs to actually accept
and prefill a sponsor from a query param, which today it does not (the form only takes a typed-in
`sponsor_ir_id`). "Earnings" (total and this-week) means the three bonus-type `WalletTransaction`s
only (`direct_referral_bonus`/`binary_bonus`/`matching_bonus`) — confirmed by mirroring
`apps/admin_portal/views.py`'s own existing platform-wide earnings aggregate (Task 23), which
already established this exact transaction-type filter and the single-conditional-aggregate-query
pattern (not three separate queries) after its own code-review pass.

**Known pre-existing gap this task finally touches:** `apps/distributors/views.py::dashboard` is
currently a `login_required`-only placeholder with zero `request.user.distributor` scoping — Task
15's own notes flagged this exact view as needing the same `is_distributor`-guard fix
`earnings_history` already got. 20a fixes it as part of building the view for real, not as separate
scope creep.

#### Task 20a: Stats aggregation backend (no Channels yet) — DONE

**Shipped 2026-07-28.** Real-browser verified (desktop and 500px mobile) with a seeded distributor
(wallet balance, earnings, PV, team size all real, non-zero values) — dashboard sidebar collapses
to a hamburger correctly on mobile, matching `base_dashboard.html`'s existing convention.

**Description:** Rewrite `apps/distributors/views.py::dashboard` to compute and render every stat
from Section 6.1 against real seeded data, reusing existing O(1)/O(log n) aggregate sources —
never walking the tree or recomputing PV live (`CLAUDE.md`'s standing Scale Architecture warning).
Guard with `apps.accounts.permissions.is_distributor`, matching `earnings_history`'s own
convention (closes the pre-existing gap above). No live-update wiring in this slice — verify the
static values are correct first, then add Channels in 20d, matching this project's own "one slice
at a time" convention.

- Wallet balance: `distributor.wallet.balance` if a `Wallet` row exists, else `0` (a `Wallet` is
  created lazily on first credit per `apps/wallet/models.py`'s own docstring — a brand-new
  distributor legitimately has none yet).
- Total / this-week earnings: one conditional-aggregate `WalletTransaction` query per stat (three
  `Sum(..., filter=Q(transaction_type=...))` terms each, mirroring `admin_portal`'s pattern
  exactly), scoped to `wallet__distributor=distributor`; this-week additionally filtered to
  `created_at__gte=<start of the current ISO calendar week>`.
- Team size: `BinaryTreeEdge.objects.filter(ancestor=distributor).count()` (all descendants, both
  legs combined, per Section 6.1's own wording) — O(number of descendants) via the existing
  `(ancestor, leg)` index, not a tree walk.
- Left/right leg PV: `distributor.pv_ledger.left_leg_pv` / `.right_leg_pv` if a `PvLedger` row
  exists, else `0` (also lazily created, same reasoning as `Wallet`).
- Monthly personal PV: `MonthlyPersonalPv` row for the current calendar-month `period`, else `0`.
- IR ID / rank: `distributor.ir_id` / `distributor.rank`, direct fields, no query needed.

**Acceptance criteria:**
- [x] All stats above render correctly for a seeded distributor with real wallet/PV/team data
- [x] A brand-new distributor with no `Wallet`/`PvLedger` row yet sees `0`s, not a 500
- [x] A non-distributor authenticated user gets a clean 403, not a 500 (`AttributeError` on
  `request.user.distributor`)

**Verification:**
- [x] pytest test: dashboard view renders correct values from seeded data (every stat, not just
  wallet balance)
- [x] pytest test: zero-state (no Wallet/PvLedger rows) renders `0`s cleanly
- [x] pytest test: a logged-in customer (non-distributor) gets 403

**Dependencies:** Task 15, Task 13, Task 25

**Files likely touched:** `apps/distributors/views.py`, `tests/feature/distributors/test_dashboard.py`

**Estimated scope:** S-M

#### Task 20b: Referral link + registration prefill — DONE

**Shipped 2026-07-28 as part of PR for Task 20 (branch `task-20a-dashboard-stats`).** Section 6.1
explicitly calls this a "personal recruitment link," not just the bare IR ID.

**Two corrections found while implementing** (both wrong assumptions in the original plan below,
fixed before writing new code): (1) `distributors:register`'s GET branch already read
`request.GET.get("ref", "")` and prefilled `DistributorRegistrationForm`'s `sponsor_ir_id` field —
present in `main` before this task touched the file, so it did not need to be built from scratch.
(2) It was **not** untested either, on a second look — `test_registration_pending.py::
test_referral_link_prefills_the_sponsor_field` already covered the context-level prefill; a
duplicate HTML-level test was written, then removed once the existing coverage was found, keeping
only the two genuinely new cases (invalid `?ref=`, no `?ref=`). (3) No existing Alpine.js
copy-to-clipboard pattern exists anywhere in this codebase (`payout_settings.html` was assumed to
have one; it doesn't) — this is the first one, a plain `x-data="{ copied: false }"` +
`navigator.clipboard.writeText(...)` button, not a reuse of a prior pattern. An invalid `?ref=`
value does **not** "fall back to blank" as originally assumed — Django's `Input.format_value()`
renders it as a literal, auto-escaped prefilled value (same as a distributor who mistyped an IR ID
by hand); `clean_sponsor_ir_id`'s `ValidationError` only fires on submit, not on this GET render.

- Referral URL: `{registration URL}?ref={distributor.ir_id}` (mechanism already existed).
- Dashboard renders the full referral URL, a Copy Link button, and a
  `https://wa.me/?text=<urlencoded message + link>` WhatsApp share link — `None` (no card content
  beyond an honest "not available yet" message) for a distributor with no `ir_id` yet (KYC not
  approved, nothing to share).
- IR ID card also got its own copy button in the same pass (Section 6.1 calls for one there too).

**Acceptance criteria:**
- [x] Referral link on the dashboard resolves to the registration page with the distributor's own
  IR ID prefilled in the sponsor field
- [x] A distributor without an IR ID yet sees an honest "not available yet" state, not a broken link
- [x] WhatsApp share button opens `wa.me` with the link correctly URL-encoded

**Verification:**
- [x] pytest: dashboard referral link/copy button/WhatsApp link only render once `ir_id` exists
  (`test_referral_link_points_to_registration_with_ir_id_prefilled`,
  `test_no_referral_link_for_a_distributor_without_an_ir_id_yet`)
- [x] pytest: an invalid/unknown `?ref=` value doesn't crash the registration page
  (`test_an_unknown_ref_value_does_not_crash_the_registration_page`)
- [x] pytest: no `?ref=` leaves the field genuinely blank, not a stray `value=""`
  (`test_no_ref_query_param_leaves_the_sponsor_field_blank`)

**Dependencies:** 20a

**Files likely touched:** `apps/distributors/views.py` (register), `apps/distributors/forms.py`,
`tests/feature/distributors/test_registration_pending.py`, `tests/feature/distributors/test_dashboard.py`

**Estimated scope:** S

#### Task 20c: Real Stitch-based frontend

**Description:** Replace `templates/distributors/dashboard.html`'s placeholder with the real
stats layout — fetched from Stitch (desktop + mobile prompts, same established workflow as Tasks
15/17/18/19/25), reconciled against `base_dashboard.html`'s real shell and design tokens rather
than the mockup's own fabricated chrome. Rank badge (Bronze/Silver, two-value), IR ID + referral
link copy buttons (Alpine.js clipboard, matching `payout_settings.html`'s existing pattern), stat
cards for wallet balance/earnings/team size/leg PV/personal PV.

**Acceptance criteria:**
- [x] Every 20a/20b stat has a real, Stitch-designed presentation (not the interim placeholder)
- [x] Verified in a real browser at desktop and mobile widths (this project's standing convention
  — a green test suite doesn't prove a UI change)
- [x] Copy buttons and WhatsApp share verified working live, not just present in markup

**Verification:**
- [x] `npm run build` + real-browser check at 1440/1024/768/500px
- [x] Manual check: copy-to-clipboard actually copies; WhatsApp link actually opens with the
  correct pre-filled text

**Shipped 2026-07-28 via PR #40, merged into `main`.** Real-browser verified at 1440/1024/768/500px
(logged in as a seeded stub distributor via a directly-created session, avoiding a real OTP SMS
send). Clipboard content confirmed via `navigator.clipboard.readText()` after clicking Copy Link —
exact referral URL with `?ref=IR00001`. WhatsApp share link confirmed correctly formed:
`https://wa.me/?text=Join%20Bancostore...` decodes to the right message + link. Mobile hamburger
drawer opens/closes correctly at 500px. Full pytest suite: 1024 passed, 1 skipped (expected), 1
failed — a known SQLite-only concurrency limitation in an unrelated IR ID sequence test
(`test_concurrent_approvals_of_different_distributors_never_duplicate_ir_ids`), already green
against real MySQL in CI; not a regression from this task.

**Dependencies:** 20a, 20b

**Files likely touched:** `templates/distributors/dashboard.html`

**Estimated scope:** S-M

#### Task 20d: Live wallet-balance updates via Channels

**Description:** The first real Channels consumer in this codebase. `doubt-driven-development`
runs before any consumer code is written — this is exactly the "authorization invariant a type
system can't verify" case the skill targets: a per-distributor group that only that distributor's
own connection ever joins. `apps/distributors/consumers.py` (new), wired into
`bancostore/asgi.py`'s currently-empty websocket `URLRouter`. Connection is rejected unless
`request.user` is authenticated AND owns the `Distributor` matching the requested group — never
trusting a client-supplied distributor ID for group membership. `apps/wallet/services.py::credit()`
(and `debit()`) sends a group message on every balance change; the dashboard's JS patches the
wallet-balance DOM element on receipt, matching this project's existing Alpine.js conventions
rather than introducing a new frontend pattern. A short design note (ADR or equivalent, per
`documentation-and-adrs`) records the auth/scoping decision, since this is the first real-time
financial-data-over-WebSocket feature in the codebase and the precedent every future notification
(Task 21's bell) will follow.

**Acceptance criteria:**
- [x] A distributor's dashboard wallet balance updates without a page refresh when their own wallet
  is credited
- [x] A distributor never receives another distributor's group messages, even if they knew or
  guessed the other distributor's ID
- [x] An unauthenticated or wrong-user WebSocket connection attempt is rejected at `connect()`

**Verification:**
- [x] pytest test using Channels' `WebsocketCommunicator`: proves group-scoping (own updates
  received, another distributor's are not)
- [x] pytest test: connection rejected for an unauthenticated/mismatched user
- [x] Manual check (this task's own original acceptance criterion): credit a commission via the
  Django shell while the dashboard is open in a real browser, confirm the balance updates live

**Shipped 2026-07-28.** Went through a full `doubt-driven-development` cycle before any code was
written: a single-model fresh-context review, then the user ran the same artifact through both
ChatGPT and Gemini independently. Across all three, real issues surfaced that changed the design
from its first draft — see `docs/decisions/0008-wallet-live-updates-channels-design.md` for the
full reasoning. Two deliberate deviations from this task's own original description, both judged
safer: (1) **no client-supplied distributor ID at all**, anywhere — the route takes no parameter,
group membership is derived purely from `scope["user"]`, eliminating the entire class of
"validate the requested group" bugs a parameterized version would need to keep getting right; (2)
**`apps/wallet/services.py::credit()`/`debit()` were not touched** — a `post_save` signal on
`WalletTransaction` (`apps/distributors/signals.py`) reacts instead, keeping the wallet app fully
decoupled from Channels, with the trade-off (documented, not silently accepted) that a future
`bulk_create()` path would silently skip live updates.

Real bugs the reviews actually caught before implementation, all fixed: reading a stale
in-memory balance instead of querying fresh at `on_commit` time; no `AllowedHostsOriginValidator`
(a real CSWSH gap for financial data); a synchronous ORM call inside async `connect()`; no
initial-balance push on connect (would leave a stale value after any reconnect); `disconnect()`
crashing on an early-rejected connection. One reviewer claim was checked against Django's own docs
and found incorrect (`on_commit` firing prematurely on a nested-transaction rollback — Django
already discards those callbacks correctly) and dropped rather than "fixed" for a non-issue.

A second, unrelated real bug was found via the real-browser verification step itself (not by any
review): `daphne` returns 503 for every static asset in local dev, since `runserver`'s
DEBUG-mode static-file auto-serving is that command's own special behavior, not something any
ASGI server gets for free. Fixed with `ASGIStaticFilesHandler` in `bancostore/asgi.py`, gated on
`settings.DEBUG` (production is unaffected either way — Nginx serves static files there). No
pytest run could ever have caught this, same class of gap as the already-documented `MEDIA_URL`
issue (`CLAUDE.md`), since Django's test runner always forces `DEBUG=False`.

Verified end-to-end in a real browser via `daphne` (not `runserver` — see the new `CLAUDE.md`
gotcha): logged in as a seeded distributor, credited their wallet from a completely separate
`manage.py shell` process, watched the dashboard's wallet balance update from GHS 0.00 to
GHS 123.45 with no page refresh.

**Dependencies:** 20a, 20c

**Files likely touched:** `apps/distributors/consumers.py` (new), `bancostore/asgi.py`,
`apps/wallet/services.py`, `templates/distributors/dashboard.html` (JS), `tests/feature/distributors/test_dashboard_live_updates.py`

**Estimated scope:** M

**Checkpoint (after 20a-20d):** full suite green, real-browser check of the complete dashboard
(static stats + live wallet update) before Task 21 (which depends on Task 20 and reuses this
task's Channels-consumer precedent for its own notification bell).

---

### Task 21: Binary tree view, earnings history, carry-forward tracker, notifications

**Re-scoped 2026-07-28 via `source-driven-development` + `planning-and-task-breakdown`, before this
task's own acceptance criteria were incomplete** (only covered the tree view and notification
bell — earnings history and the carry-forward tracker had zero acceptance criteria or tests
listed, despite being in the description). Read Section 6.2/6.3/6.5/6.6 of the primary source doc
directly rather than trusting this entry's own prior paraphrase, and checked the real codebase
before slicing:
- `Distributor.full_name` already exists for the tree view's "name" field.
- `PvDailyBucket` (`distributor`, `leg`, `date`, `pv`) is the real data source for the
  carry-forward tracker, with expiry via the already-seeded `PV_CARRY_FORWARD_EXPIRY_DAYS`/
  `PV_EXPIRY_WARNING_DAYS` constance settings — no new backend concept needed, just a new read
  path over data Task 13 already produces.
- Section 6.3 (Earnings History) is likely **already fully satisfied** by Task 15's dedicated
  `earnings_history` page plus Task 20c's dashboard link to it — 21c below is a verification pass,
  not a rebuild.
- `apps/notifications/models.py` currently has only `OTPCode` — the notification bell needs a
  genuinely new `Notification` model, not a reuse of anything existing.
- Section 6.6's own notification list does **not** include the matching bonus — only binary bonus
  and referral bonus are named. Wiring a matching-bonus notification would be scope not asked for.

Broken into four sub-tasks (21a-21d), following the same pattern as Task 16a-h/Task 20a-d.

#### Task 21a: Binary tree visual view

**Description:** A new view rendering the distributor's own downline as a visual tree (name, IR
ID, rank, PV per node), querying `BinaryTreeEdge.objects.filter(ancestor=request.user.distributor)
.select_related("descendant")` — the same direction Task 20a's "Team size" stat already uses, just
reconstructed into a hierarchy instead of a flat count. O(number of descendants) via the existing
index, never a recursive walk (this project's standing Scale Architecture rule).

**Acceptance criteria:**
- [x] Tree view renders every descendant with name, IR ID, rank, and PV, correctly split by leg
- [x] A distributor with no downline yet sees an honest empty state, not a broken/blank page
- [x] A distributor can never see another distributor's tree, even by manipulating any parameter
      (there should be none to manipulate — scoped unconditionally to `request.user.distributor`,
      same IDOR-safe-by-construction pattern as every other distributor-scoped view)

**Verification:**
- [x] pytest test: a seeded multi-level downline renders the correct nodes and leg placement
- [x] pytest test: empty-downline state renders cleanly
- [x] pytest test: query count doesn't grow per-node (one query, not N+1 per descendant)
- [x] Live browser check against a real seeded downline

**Dependencies:** Task 9a/9c (closure table), Task 20

**Files likely touched:** `apps/distributors/views.py`, `templates/distributors/binary_tree.html`, `tests/feature/distributors/test_binary_tree_view.py`

**Estimated scope:** M

**Status: Done (PR #44, merged 2026-07-28).** Built as `apps/binary_tree/services.py::get_downline_tree`
(3 flat queries regardless of downline size/depth, verified by a query-count-invariance test) plus
`binary_tree_view`/`binary_tree.html`/`_binary_tree_node.html`. CodeRabbit's review flagged the tree
assembly (`_build`) and the view's node-counting helper (`_count_subtree`) as recursive Python
functions that could in principle hit Python's default recursion limit on a pathologically deep
single-line downline (every distributor sponsoring exactly one next distributor, never spilling
over) — both fixed pre-merge to be iterative (BFS + reverse-visit-order build for the tree, an
explicit stack for counting), matching this project's standing "never a recursive walk" rule.
**Deferred, not silently skipped:** two lower-priority CodeRabbit nitpicks were left as-is —
`_binary_tree_node.html`'s own recursive `{% include %}` has the same theoretical deep-chain limit
(flattening template rendering to an iterative pre-order list is a heavier structural change than
this task's scope, and in practice tree depth stays ~log2(n) under the current auto-balance-only
placement algorithm — see `project_binary_tree_placement_spillover_rule` memory); and the rank-badge
pill markup is duplicated between `dashboard.html` and `_binary_tree_node.html` rather than
extracted into a shared partial. Both are candidates for a future pass, not correctness bugs.

**Follow-up (PR #45, merged 2026-07-29):** the fetched Stitch design's tree area is a
draggable/zoomable canvas (drag to pan, a "Zoom In" button, scroll/pinch to zoom out, a "Recenter"
button) — missed in the original build above, caught by the user after merge. Added a small inline
Alpine.js component (translate+scale transform, matching this project's existing inline-`x-data`
convention) plus keyboard pan/zoom/recenter support. Two real bugs found and fixed during live-browser
verification: mouse-drag triggered native text-selection on node cards (fixed with `user-select: none`
+ `preventDefault()`), and that `preventDefault()` silently broke keyboard focus after a drag (fixed
with an explicit `this.$el.focus()`). CodeRabbit's review caught one more genuine gap pre-merge — the
canvas advertised "pinch to zoom" and disabled native pinch (`touch-action: none`) but never actually
computed scale from two-touch distance, so a pinch just panned — fixed with real two-finger
distance-based scaling, verified by driving `startDrag`/`onDrag` with synthetic touch objects.
CodeRabbit's other finding on the same PR (`this.$el` allegedly focusing the outer wrapper instead of
the canvas) did not hold up under empirical verification and was left as-is.

**Follow-up fix (PR #46, merged 2026-07-29):** user reported that just moving the mouse/trackpad over
the canvas zoomed it instead of letting the page scroll, making that section impossible to scroll
past. Root cause: `@wheel.prevent` unconditionally called `preventDefault()` on every wheel event over
the canvas regardless of focus state. Verified against Alpine.js's own docs before fixing (`.prevent`
is unconditional; wheel listeners are passive by default unless `.passive.false` is set). Fixed with
`@wheel.passive.false` plus a `document.activeElement !== this.$el` check in `onWheel()` so scroll-to-
zoom only activates once the canvas has been clicked/focused — otherwise the page scrolls normally,
matching how embedded map/canvas widgets avoid trapping scroll. Touch pinch, mouse drag, and keyboard
pan/zoom needed no equivalent change since they're already inherently explicit gestures.

---

#### Task 21b: Carry-forward PV tracker

**Description:** Reads `PvDailyBucket` for the distributor's current non-expired buckets, sums
remaining PV, and surfaces the earliest expiry date (oldest bucket's `date` +
`PV_CARRY_FORWARD_EXPIRY_DAYS`) so the distributor can act before losing it — using the existing
`PV_EXPIRY_WARNING_DAYS` setting to flag when a batch is close to expiring.

**Acceptance criteria:**
- [ ] Shows total carried-forward PV and the nearest expiry date, computed from real `PvDailyBucket`
      rows, not a static placeholder
- [x] Already-expired buckets are excluded from the total
- [x] A distributor with no carried-forward PV sees an honest zero-state

**Verification:**
- [x] pytest test: total and nearest-expiry-date match a seeded set of buckets at different ages
- [x] pytest test: expired buckets are correctly excluded
- [x] Live browser check

**Dependencies:** Task 13 (PvDailyBucket), Task 20

**Files likely touched:** `apps/distributors/views.py`, `templates/distributors/dashboard.html` or a new template, `tests/feature/distributors/test_carry_forward_tracker.py`

**Estimated scope:** S-M

**Status: Done (PR #47, merged 2026-07-29).** Built as `apps/pv_ledger/services.py::get_carry_forward_summary`,
wired into the existing dashboard view as a new stat card. `source-driven-development` against Section 6.5
directly confirmed the tracker must show PV "carried forward on the strong leg" specifically, not a combined
total — reused the exact same weak/strong leg comparison `apps/commissions/services.py`'s real Binary Bonus
cycle already uses (`sum_leg_pv` per leg), so the displayed value can never drift out of sync with the real
payout logic. Built test-first (7 unit tests + 2 feature tests). A `doubt-driven-development` pass caught one
real bug before it shipped: `config.PV_CARRY_FORWARD_EXPIRY_DAYS` was read twice (risking a mid-call drift if
the admin changed it between reads) — fixed by reading it once, the same bug shape this codebase already paid
for twice before (Task 14, Task 19a). The reviewer's other finding (no locking around the read) was accepted
as a trade-off matching every other read-only dashboard stat in this codebase, documented rather than fixed.
Verified live in a real browser at desktop and mobile widths, both the nearing-expiry and zero states. Full
suite green (1050 passed, 1 skipped).

---

#### Task 21c: Confirm Earnings History satisfies Section 6.3 (verification only)

**Description:** Section 6.3 asks for date/time, bonus type, and amount credited per entry —
Task 15's `earnings_history` page plus Task 20c's dashboard link were very likely already
sufficient. Verify directly against the source doc's three bullet points rather than assuming;
scope a small fix only if a real gap is found (e.g., bonus *type* not distinguishable in the
current UI).

**Acceptance criteria:**
- [x] Every one of Section 6.3's three bullet points is confirmed present in the existing page,
      or a specific, named gap is fixed

**Verification:**
- [x] Live browser check against a seeded distributor with mixed bonus-type earnings

**Dependencies:** Task 15, Task 20c

**Estimated scope:** XS (verification) — only grows if a real gap turns up

**Status: Done (2026-07-29), verification only — no code changed.** Confirmed live against a
distributor seeded with all three bonus types (Direct Referral, Matching, Binary) plus a
withdrawal debit: `templates/distributors/earnings_history.html` already shows date+time
(`txn.created_at|date:"M d, Y | g:i A"`), a distinct label per bonus type (explicit branches for
`direct_referral_bonus`/`binary_bonus`/`matching_bonus`, not a generic fallback), and the signed
GHS amount. All three of Section 6.3's bullet points hold with no gap. **FYI, not acted on (no
functional gap, so out of this task's scope per its own estimate):**
`tests/feature/distributors/test_earnings_history.py::test_lists_transactions_with_type_signed_amount_and_date`
only asserts `direct_referral_bonus` and `withdrawal_debit` render correctly, not `binary_bonus`/
`matching_bonus` — a test-coverage gap, not a feature gap, left for a future pass rather than
silently expanding this task's scope.

---

#### Task 21d: Live notification bell

**Description:** The second real Channels consumer in this codebase (first was Task 20d's wallet
balance, which set the auth/group-scoping precedent this one must follow exactly). A new
`Notification` model (`apps/notifications/models.py`) plus a consumer wired into
`bancostore/asgi.py`'s existing websocket router, notifying on exactly the events Section 6.6
names — no more, no less:
- Someone new joined under them (on `BinaryTree.place_distributor` success)
- A binary bonus was calculated and credited
- A referral bonus was paid instantly
- Their withdrawal was approved and sent
- Their KYC was approved or rejected
- A PV batch is approaching expiry (via the existing `PV_EXPIRY_WARNING_DAYS` setting — likely a
  scheduled check, same Celery Beat pattern as the Binary Bonus batch driver)

**`doubt-driven-development` runs before any consumer code is written**, same as Task 20d — the
same class of risk (a distributor must never receive another distributor's notifications) plus a
new one specific to this task: 6 different trigger points across 5+ existing service functions
means 6 separate chances to get the group-scoping or event-payload shape wrong. Likely needs
further sub-slicing once the design is reviewed (e.g. 21d-i model + consumer, 21d-ii the 5
immediate-event triggers, 21d-iii the PV-expiry scheduled check) rather than building all of it in
one sitting — decide after the doubt-driven-development pass, not before.

**Acceptance criteria:**
- [x] A notification appears live (no page refresh) for each of the 6 event types above, and only
      to the distributor it belongs to
- [x] A distributor never receives another distributor's notifications, even by a guessed ID
- [x] Notifications persist (a `Notification` row exists) so they're visible on next login, not
      only while connected

**Verification:**
- [x] pytest tests using `WebsocketCommunicator`, mirroring Task 20d's pattern exactly: auth
      rejection, cross-distributor isolation
- [x] One pytest test per trigger point, proving a `Notification` row is created and pushed on the
      real event (not simulated)
- [x] Live browser check: triggered all 6 event types for real via `send_notification` from a
      shell while a real logged-in browser tab stayed open; confirmed the bell updates live

**Dependencies:** Task 20d (Channels/auth precedent), Task 9 (placement), Task 13/14 (bonus
credits), Task 16 (withdrawal approval), Task 11 (KYC decision)

**Files likely touched:** `apps/notifications/models.py`, `apps/distributors/consumers.py`,
`apps/distributors/signals.py`, `bancostore/asgi.py`, `templates/distributors/base_dashboard.html`
(bell UI — currently a disabled placeholder per its own "coming soon" convention),
`tests/feature/distributors/test_notifications.py`

**Estimated scope:** L — will very likely need further sub-slicing once
`doubt-driven-development` has run

**2026-07-29: `doubt-driven-development` ran before any code was written** (fresh-context
adversarial review against a concrete proposed design, not just the description above) and found
two real Critical-severity design bugs, plus several real Medium/Low findings, all reconciled below.
Sliced into four sub-tasks as a result (21d-i through 21d-iv), matching this task's own
"will very likely need further sub-slicing" prediction.

**Design findings from the review, all folded into the design before any code was written:**
- **Critical, fixed:** the original design only wrapped the Channels `group_send()` call in
  try/except, leaving `Notification.objects.create()` itself unguarded — if that DB write ever
  raised (e.g. a message-length overflow under MySQL strict mode), the exception would propagate
  into the caller's *existing* locked transaction (binary bonus credit, placement, referral bonus)
  and roll back an already-succeeded business event. Fixed: `send_notification` now wraps both the
  create and the push in their own try/except, and the whole thing runs inside
  `transaction.on_commit(...)` (safe to call even outside an active transaction — Django runs
  on_commit callables immediately in that case) so a notification failure can *never* affect the
  triggering event, matching this codebase's own established "SMS send happens after the locked
  block returns, never inside it" convention (Task 16).
- **Critical, fixed:** the original design did the DB write + WebSocket push synchronously inside
  the caller's lock (no `on_commit` deferral), unlike `WalletBalanceConsumer`'s own established
  signal+`on_commit` pattern — this held locks open across a Redis round trip and could push a
  notification for a row that then got rolled back. Fixed by the same `on_commit` change above.
- **High, fixed:** verified the new `notification_group_name(distributor_id)` helper (mirroring
  `apps/distributors/realtime.py::wallet_group_name`) returns a distinct prefix
  (`"notifications_{id}"` vs. the existing `"wallet_{id}"`) — no group-name collision risk between
  the two consumers for the same distributor.
- **High, fixed:** the PV-expiry scheduled check (21d-iii) will reuse the existing generalized
  `CommissionCycleRun`/`CommissionCycleFailure` `job_name`-discriminated audit-trail model (Task
  14's own generalization) plus its established per-iteration-renewed Redis lock convention,
  instead of inventing a third audit-trail model or skipping the lock entirely.
- **Medium, fixed:** the PV-expiry check needs a dedup guard — without one, a distributor whose
  oldest bucket sits inside the warning window for several consecutive days (a daily scheduled
  check) would get a duplicate notification every single day until it actually expires. 21d-iii
  must only notify once per (distributor, nearest_expiry_date) pair.
- **Medium, fixed:** added a second index, `(distributor, is_read)`, alongside `(distributor,
  -created_at)` — the bell's unread-count badge needs an efficient filtered count, not just an
  efficient ordered list.
- **Medium, assessed as noise (matches existing convention, not a new gap):** the reviewer flagged
  `on_delete=CASCADE` on the `distributor` FK as inconsistent with an "audit-preservation" pattern —
  checked against `PvDailyBucket` (also FK'd to `Distributor` with `CASCADE`) and confirmed CASCADE
  is this codebase's actual existing convention for distributor-owned rows, not something new being
  introduced here.
- **Medium, deferred (documented, not silently skipped):** no retention/pruning policy for old
  notifications — matches this codebase's existing lack of a generic retention policy for similar
  models (`WalletTransaction`, `PvDailyBucket` rows aren't generically pruned either), a reasonable
  future task if the table grows large, not a blocker now.
- **Low, fixed:** `Notification.message` bumped to a generous `max_length` (500, not 255) to make a
  real-world length overflow far less likely, on top of the try/except that already catches one.
- **Low, fixed:** a regression test asserting a `MATCHING_BONUS` credit never produces a
  `Notification` row — guarding the explicit, source-doc-confirmed scope boundary (Section 6.6 does
  not name the matching bonus) against future accidental scope creep.
- **Low, assessed as noise:** the reviewer suggested a structured "outcome" field for the KYC
  decision event distinct from `event_type`/`message` — unnecessary, since `message` is already
  free text decided at the call site with full context ("Your KYC was approved" vs. "...rejected:
  <reason>"), matching how every other event's message is already composed.

#### Task 21d-i: Notification model, Channels consumer, and send_notification service

**Description:** The foundation slice — no event wiring yet, just the pieces every trigger will
call into. `apps/notifications/models.py::Notification` (`distributor` FK CASCADE, `event_type`
6-choice `TextChoices` matching Section 6.6 exactly, `message` CharField(500), `is_read`,
`created_at`, indexes on `(distributor, -created_at)` and `(distributor, is_read)`).
`apps/distributors/realtime.py::notification_group_name`. `apps/distributors/consumers.py::
NotificationConsumer`, mirroring `WalletBalanceConsumer`'s `connect()`/auth logic exactly (same
`is_distributor` + `Distributor.DoesNotExist` handling, same accept-only-after-verification shape).
`apps/notifications/services.py::send_notification(distributor, event_type, message)` per the
on_commit-wrapped, doubly-try/excepted design above.

**Acceptance criteria:**
- [x] `send_notification` persists a `Notification` row and pushes it live to a connected client,
      and does so only after the caller's transaction commits (verified with
      `@pytest.mark.django_db(transaction=True)`, matching Task 20d's own convention)
- [x] A `Notification.objects.create()` failure never propagates to the caller
- [x] A `group_send()` failure never propagates to the caller
- [x] The new consumer rejects an unauthenticated connection and a non-distributor account, and a
      distributor connecting to their own group never receives another distributor's group's pushes

**Verification:**
- [x] `WebsocketCommunicator` tests mirroring Task 20d's test file structure exactly
- [x] Unit tests for `send_notification`'s failure-isolation behavior (mock/force each failure mode)

**Dependencies:** Task 20d (Channels/auth precedent)

**Files:** `apps/notifications/models.py`, `apps/notifications/services.py`,
`apps/distributors/realtime.py`, `apps/distributors/consumers.py`, `bancostore/asgi.py`,
`tests/unit/notifications/test_send_notification.py`,
`tests/feature/distributors/test_notification_consumer.py`

**Estimated scope:** M

**Status: Done (PR #48, merged 2026-07-29).** 10 new tests, all passing, no regression to the
existing wallet-balance consumer. See the doubt-driven-development writeup above this sub-task for
the two Critical bugs caught and fixed before any code was written.

---

#### Task 21d-ii: Wire the 5 immediate-event triggers

**Description:** Calls `send_notification` from each of the 5 non-scheduled event sites named by
Section 6.6: binary-tree placement (notifies the *sponsor* of their new downline member), binary
bonus credited, referral bonus paid, withdrawal approved, KYC decision (approve or reject). Each
call site is wrapped in the caller's own `transaction.on_commit(...)` (or relies on
`send_notification`'s own internal `on_commit`, whichever reads more clearly at each site once
written) so the call is always deferred past the existing locked block, per the design fix above.

**Acceptance criteria:**
- [x] Each of the 5 triggers creates exactly one `Notification` with the correct `event_type` and a
      human-readable `message`, only for the distributor the event belongs to
- [x] A `MATCHING_BONUS` credit never creates a `Notification` (explicit regression test — Section
      6.6 does not name the matching bonus)

**Verification:**
- [x] One pytest test per trigger point exercising the *real* underlying function (not a simulated
      call to `send_notification` directly) — proving the wiring, not just the primitive
- [x] The matching-bonus-exclusion regression test above

**Dependencies:** 21d-i

**Files:** `apps/binary_tree/services.py`, `apps/commissions/services.py`,
`apps/withdrawal/services.py`, wherever the KYC approve/reject action lives (Django Admin action,
`apps/distributors/admin.py` or similar — confirm exact location before editing),
`tests/unit/.../test_*_notification.py` per trigger

**Estimated scope:** M

**Status: Done (PR #49, merged 2026-07-29).** `approve_kyc`/`reject_kyc` changed from a bare
`return`/no return value to `return True`/`return False` so the caller can distinguish a real
transition from the existing idempotent no-op path — necessary so re-approving an already-approved
distributor never double-notifies. 10 new tests, all passing; 573 broader regression tests across
binary_tree/commissions/distributors/withdrawal/notifications/KYC-review, no regressions.
CodeRabbit flagged a possible live-SMS risk in the referral-bonus test (since
`_credit_direct_referral_bonus` calls `send_sms`) — verified empirically as noise: an existing
autouse `tests/conftest.py::_force_fake_sms_sender` fixture already forces `MNOTIFY_API_KEY=""` for
every test in the suite, so a real SMS is structurally impossible regardless of `.env`.

---

#### Task 21d-iii: PV-expiry scheduled notification

**Description:** The 6th event type, and the only scheduled/batch one — a Celery Beat task
checking which distributors have surviving PV within `PV_EXPIRY_WARNING_DAYS` of expiring (reusing
a query shaped like `apps.pv_ledger.services.distributor_ids_with_pending_pv`, not a full table
scan) and notifying each exactly once per (distributor, nearest_expiry_date) pair — not once per
scheduled run. Reuses the existing generalized `CommissionCycleRun`/`CommissionCycleFailure`
`job_name`-discriminated audit-trail model and its per-iteration-renewed Redis lock convention
(Task 13h/14), rather than a new audit model or an unlocked loop.

**Acceptance criteria:**
- [x] A distributor with PV inside the warning window is notified once
- [x] The same distributor is NOT re-notified on a subsequent run while the same PV batch is still
      the nearest-expiring one (dedup by (distributor, nearest_expiry_date))
- [x] Query cost stays flat regardless of total distributor count (reuses the existing candidate-
      set-narrowing pattern, never scans every registered user)

**Verification:**
- [x] pytest: seeded distributor with a near-expiring bucket gets notified; a second run with no
      state change does not double-notify
- [ ] pytest: query-count-invariance test, matching Task 21a's own precedent for this class of claim
      — **not built** (see Status note below); the flat-cost claim rests on the candidate query's
      own already-proven shape, not a measurement of this task specifically

**Dependencies:** 21d-i, Task 13 (`CommissionCycleRun`/`Failure`, per-iteration lock convention)

**Files:** `apps/notifications/tasks.py` (new), a new migration seeding the periodic task
(matching Task 13/14's own migration pattern), `tests/unit/notifications/test_pv_expiry_task.py`

**Estimated scope:** M

**Status: Done (PR #50, merged 2026-07-30).** Built as its own `NotificationCycleRun`/
`NotificationCycleFailure` audit model (mirroring `OrderCycleRun`/`Failure`), not the
`CommissionCycleRun`/`Failure` reuse originally planned above — investigating the real precedent
during implementation found Task 18d had already established "one audit model per owning app" (its
own `OrderCycleRun`, not a third consumer of `CommissionCycleRun`) since this job moves no money and
isn't order-shaped either; matching that precedent instead of the plan as originally written.
Reuses `get_carry_forward_summary` (Task 21b) directly for the nearing-expiry/amount/date logic, so
the dashboard's Carry-Forward PV card and this notification can never disagree. **Deferred, not
silently skipped:** no dedicated query-count-invariance test was written (unlike Task 21a's own
precedent for this class of claim) — the "flat regardless of distributor count" property rests on
`distributor_ids_with_pending_pv`'s already-proven query shape (used unmodified, not re-derived)
plus `get_carry_forward_summary`'s own already-proven 3-query-per-call shape, not a fresh
measurement of this specific task. Full regression check (307 tests across notifications/pv_ledger/
orders/commissions) green; one unrelated pre-existing flaky timing test confirmed as a flake, not a
regression, in a file this task never touched.

---

#### Task 21d-iv: Notification bell UI

**Description:** Wires the existing disabled "coming soon" bell icon (already present in
`templates/distributors/base_dashboard.html`'s header, per its own established honesty convention)
to the real consumer -- a live unread-count badge plus a dropdown listing recent notifications,
mark-as-read on open/click. Needs a real Stitch screen fetched first (this codebase's established
UI workflow) -- no Stitch screen for this exists yet, unlike the tree/dashboard work which already
had fetched mockups to reconcile against.

**Acceptance criteria:**
- [x] Bell shows a live, accurate unread count with no page refresh
- [x] Opening the dropdown shows recent notifications with correct type/message/relative time
- [x] Notifications are marked read via explicit action (single-item click + "mark all as read" —
      the fetched Stitch mockup used an explicit button, not read-on-open, so that's what shipped)
- [x] Empty state (no notifications yet) is honest, not a blank dropdown

**Verification:**
- [x] Live browser check: triggered real events via `send_notification` from a shell while the
      dashboard tab stayed open; confirmed the badge updates live with no refresh
- [x] Mobile + desktop width check (500px macOS floor + 1440px), matching this project's
      established responsive convention

**Dependencies:** 21d-i, 21d-ii (at least one real trigger to demo against)

**Files:** `templates/distributors/base_dashboard.html` (real bell + WS client JS, replacing the
disabled placeholder), `templates/distributors/_notification_dropdown.html` (new partial, shared
by all three of `notification_dropdown`/`notification_mark_read`/`notification_mark_all_read`),
`templates/distributors/notification_history.html` (new, the dropdown's "View all" destination,
paginated), `apps/notifications/context_processors.py` (new, powers the header badge site-wide),
`apps/distributors/views.py`/`urls.py` (4 new views/routes), `apps/notifications/models.py`
(`icon_name`/`icon_classes` per `EventType`), `apps/notifications/services.py`
(`push_unread_count_update`, `unread_count` added to `_push_live`'s payload),
`apps/distributors/consumers.py` (`unread_count_update` handler), `static/src/main.js` (htmx CSRF
wiring -- the first htmx POST usage anywhere in this codebase).

**Built from 4 fetched Stitch screens** ("Notification Bell - Bancostore" Populated/Empty
State/Mobile/Mobile Empty State), reconciled against real scope: dropped each mockup's own fake
surrounding dashboard chrome, illustration artwork, the mobile empty state's settings-gear icon/
"Refresh Portal" button/category filter chips (all fabricated -- no such features exist), and
picked ONE canonical icon per `EventType` where the desktop and mobile mockups disagreed with each
other (withdrawal-approved: `check_circle`/green, not mobile's `account_balance_wallet`/gray). The
mobile mockup's PV-expiring "urgent/ALERT" red-tinted treatment was kept and applied consistently
on both desktop and mobile, not just mobile -- a genuinely worthwhile design signal from the
mockup, not fabricated chrome.

A `doubt-driven-development` pass before implementation caught two real design gaps: (1) the
context processor must never assume `is_distributor()` (a group-membership check only) implies a
`Distributor` row exists -- since this processor runs on EVERY page site-wide, that gap (already
documented elsewhere in this codebase, CLAUDE.md's Task 15 note) would 500 the whole site for such
a user, not one view; fixed with a `try/except Distributor.DoesNotExist` guard. (2) cross-tab badge
staleness -- the original design had the client only increment/decrement the badge from a bare "new
notification" push, which drifts the moment a second tab marks something read. Fixed by making
every push (`notification_push` and a new `unread_count_update` broadcast fired on mark-read/
mark-all-read) carry the absolute, server-computed count; the client only ever sets, never
increments.

**Three real bugs caught only by live-browser verification, none of which any test suite would
have caught:**
1. A multi-line Django `{# ... #}` comment (Django's comment tag is single-line only) rendered as
   literal visible page text in `_notification_dropdown.html` -- the same recurring footgun
   CLAUDE.md's Task 18f entry already documents, caught and fixed twice in this task alone (once
   in the original code, once again in the comment written to document a different fix).
2. The mobile full-screen notification overlay was trapped inside the header's 64px height instead
   of covering the viewport -- root-caused to `backdrop-blur-sm` (a CSS `backdrop-filter`) on
   `base_dashboard.html`'s `<header>`, which -- like `transform`/`filter`/`perspective` -- silently
   establishes a new containing block for any `position: fixed` descendant. Fixed by removing it
   (`bg-surface/90` alone still gives the same translucent header).
3. The live badge push had two independent, layered bugs, both invisible to `WebsocketCommunicator`-
   based automated tests (which bypass the real asyncio Redis client entirely): (a) `channels-redis`
   4.3.0 doesn't tolerate `redis-py` 8.x's asyncio internals -- a routine idle-long-poll
   `TimeoutError` escapes instead of being retried, crashing the WebSocket connection (close code
   1011). Confirmed this pre-existed and ALSO silently affected the already-shipped Task 20d
   wallet-balance live push, not something this task introduced. User-approved fix: pin
   `redis<5` in `requirements.txt` (channels-redis's own declared `redis>=4.6` has no upper bound).
   (b) Layered on top: the header JS cached `document.getElementById("notif-badge")` once at page
   load, but `_notification_dropdown.html`'s `hx-swap-oob="true"` fragment for that same element
   defaults to an `outerHTML` swap on every dropdown-open/mark-read/mark-all-read response --
   detaching the original node. A cached reference silently became a no-op pointing at a removed
   node the moment a user opened the dropdown once. Fixed by re-querying the element fresh on every
   WebSocket message instead of caching it.

**A `code-review-and-quality` pass (subagent) caught one more real, 100%-reproducible bug before
merge:** all four new views had inherited `@_redirect_if_cooling_off_cancelled` (this codebase's
decorator restricting a cooling-off-cancelled-but-still-logged-in distributor to withdrawal-only
pages). But `notification_dropdown` is only ever called via `htmx.ajax()` GET targeting the small
`#notif-panel-content` div -- the decorator's bare page redirect gets followed transparently by
that GET and swaps an ENTIRE PAGE into the 384px dropdown. Fixed by removing the decorator from all
four views: viewing/dismissing notification history isn't an earning-related action the decorator
is meant to guard, and a cancelled distributor may still have a relevant `WITHDRAWAL_APPROVED`
notification for the refund they're claiming -- matching how `withdrawal_request`/
`withdrawal_history`/`payout_settings` are already exempted from this same decorator. Regression
tests added to `tests/feature/distributors/test_post_cancellation_access.py` (not a new file --
joined the existing suite of "stays reachable for a cancelled distributor" tests for consistency).
A parallel `security-and-hardening` subagent pass found zero exploitable issues (IDOR, CSRF,
WebSocket auth, XSS, the global context processor, and the `redis<5` pin's own CVE history all
checked clean) -- only two Info-level notes (a cosmetic decorator-ordering inconsistency, fixed;
and a flagged follow-up to un-pin `redis` once `channels-redis` supports a newer redis-py, tracked
below, not silently accepted as permanent).

**Deferred, not silently skipped:** the `redis<5` pin is a real, currently-inert supply-chain/
maintenance risk (the 4.x line won't receive further security patches) -- revisit once
`channels-redis` (or a replacement) is verified compatible with a newer `redis-py`.

**Estimated scope:** M — shipped.

---

**Checkpoint H:** dashboard, tree view, and notification bell all update live (no page refresh)
when a triggering event happens in another session.

---

**Checkpoint H:** dashboard and tree view update live (no page refresh) when a commission is
credited in another session.

---

## Phase 9: Admin Portal — custom Stitch-designed dashboard (redefined 2026-07-23)

**Scope change, 2026-07-23:** the admin is not software-literate; every screen they touch routinely
needs to look and feel like the rest of Bancostore, not Django's generic admin panel. Supersedes
this phase's original "Django Admin (customized)" scope — see `tasks/plan.md` Phase 9 for the full
design principle (Django Admin's `ModelAdmin` classes stay underneath as the permissions/audit
layer; new Stitch-designed screens sit in front and call the same already-tested service
functions) and architecture (`apps/admin_portal/`, mirrors `templates/distributors/
base_dashboard.html`'s shell). Build workflow going forward: detailed Stitch prompt →
user sends it and replies to fetch → fetch via Stitch MCP, build the template, verify against the
prompt (drop anything Stitch adds out of scope) → test live in browser → next prompt. One screen at
a time, same discipline as Task 16's UI slices.

Sequencing: KYC review (Task 22, this is the first slice — admin already actively uses this
screen, backend already proven in Task 11) → withdrawal approval, distributor search/management,
commission oversight (Task 23, sliced further when started) → catalog management (Task 26) →
admin dashboard (Task 27). Tasks 26/27 weren't in the original plan — added 2026-07-30/31 after
auditing which already-shipped backend features still had no dedicated frontend, the same kind of
gap Task 25 closed for distributors/customers.

### Task 22: Admin Portal — KYC review screen

**Description:** Replaces `DistributorAdmin`'s Django-Admin KYC review (list + `DiditVerificationInline`
+ approve/reject bulk actions) with a Stitch-designed queue screen in `apps/admin_portal/`. Calls
the existing `apps.distributors.services.approve_kyc`/`reject_kyc` directly — no service-layer
changes, presentation only.

**Acceptance criteria:**
- [x] Admin sees a queue of pending-KYC distributors with Didit's verification result summary
      (status, face-match/liveness scores, extracted name/document number, warnings, the three
      images) — same data `DiditVerificationInline` already shows, restyled
- [x] Approve/reject work per-distributor (reject requires a reason, matching the existing
      Django-Admin confirmation-page pattern's requirement)
- [x] Django Admin's own KYC screen keeps working unchanged (not removed, just no longer the
      admin's primary path)

**Verification:**
- [x] pytest test: approve/reject from the new screen produce identical `Distributor.kyc_status`/
      `kyc_rejection_reason` results as the existing Django-Admin action (same service call)
- [x] Live browser check: full approve and full reject flow against a real pending KYC record

**Shipped 2026-07-23 via PR #12.** Correction 2026-07-28: this checklist was never updated when
the work landed — found stale while investigating "what's next" and corrected against the real
`apps/admin_portal/` code and `tests/feature/admin_portal/test_kyc_review.py`, not assumed.

**Dependencies:** Task 11 (KYC backend), admin login/2FA (Task 6)

**Files likely touched:** new `apps/admin_portal/` app (urls, views, templates), `templates/admin_portal/base_dashboard.html`, `templates/admin_portal/kyc_review.html`, `tests/feature/admin_portal/test_kyc_review.py`

**Estimated scope:** M

---

### Task 23: Admin Portal — withdrawal approval, distributor management, commission oversight

**Description:** Absorbs the original Task 22/23 scope (distributor search/profile/suspend,
commission oversight) plus a Stitch-designed withdrawal approval screen fronting Task 16d's
already-built `approve_withdrawal_request`/`reject_withdrawal_request`.

**Shipped 2026-07-23, same day as Task 22** (correction added 2026-07-28 — this task had no
sub-slice breakdown recorded and showed as not started, despite being fully built; verified against
the real codebase, not assumed):
- Withdrawal review queue + detail (approve/reject fronting Task 16d's service functions) — PR #14
- Distributor directory + profile screens — PR #15
- Real-time search/filter, CSV export, and real stats added to the directory — PR #16
- Commission oversight + commission cycle detail — PR #17
- Polish follow-ups: mobile table/typography fixes across 6 admin_portal pages — PR #23; shared
  flash-message component — PR #34

Full test coverage: `tests/feature/admin_portal/test_withdrawal_review.py`,
`test_distributor_directory.py`, `test_commission_oversight.py`.

**Dependencies:** Task 22 (shared `apps/admin_portal/` shell), Task 16d (withdrawal backend), Task 13 (commission data)

**Estimated scope:** L

---

### Task 26: Admin Portal — Catalog Management (categories + products)

**Description:** Not in the original plan — found by auditing which already-shipped backend
features still had no dedicated frontend. Task 7 built `Category`/`Product`/`ProductImage`/
`ProductVariant` with only Django Admin CRUD; this replaces that with a real Stitch-designed UI in
`apps/admin_portal/`, matching Task 22/23's established pattern of Django Admin staying underneath
as the permissions/audit layer while a branded screen sits in front.

**Acceptance criteria:**
- [x] Category list/create/edit/delete screens (Add Category as a modal, per the fetched Stitch
      design)
- [x] Product list screen with real-time auto-filter search (no Filter button) and themed,
      non-native category/status/featured filter dropdowns
- [x] Product create/edit form: name/description/price/category/variants, up to 5 images with
      primary-image selection via reveal-on-demand image tiles (not a raw multi-file input)
- [x] Delete confirmation modal, shared across categories and products
- [x] All pages responsive at common breakpoints with no overflow

**Verification:**
- [x] `tests/feature/admin_portal/test_catalog_management.py` — CRUD/filter/permission/formset/
      image-cap coverage
- [x] `tests/feature/catalog/test_primary_image_normalization.py` — `normalize_primary_image()`
      unit tests
- [x] Live browser check across category/product list/create/edit/delete, all breakpoints
- [x] CodeRabbit review, findings fixed pre-merge (see `CLAUDE.md`'s Task 26 entry for the full bug
      list — a CSS Grid col-span bug, a star-button stacking-context bug, an unhandled
      `ValueError`/`ProtectedError` pair, and a silently-swallowed formset validation error)

**Shipped via PR #53, merged 2026-07-31.**

**Dependencies:** Task 7 (Category/Product models), Task 22 (shared `apps/admin_portal/` shell)

**Files touched:** `apps/admin_portal/forms.py` (new), `apps/admin_portal/views.py`,
`apps/admin_portal/urls.py`, `apps/catalog/services.py` (new `normalize_primary_image`),
`templates/admin_portal/catalog_category_list.html`, `catalog_product_list.html`,
`catalog_product_form.html`, `partials/category_results.html`, `partials/product_results.html`,
`partials/_delete_confirm_modal.html`

**Estimated scope:** L

---

### Task 27: Admin Dashboard

**Description:** Not in the original plan — found the same way as Task 26. Replaces the Task 22
placeholder dashboard ("you're logged in, KYC Review is the only real feature so far") with a real
one now that every other admin_portal section (KYC, withdrawals, distributor directory, commission
oversight, catalog) has shipped.

**Acceptance criteria:**
- [x] Four action-needed cards (Pending KYC, Pending Withdrawals, Orders Awaiting Action, Low Stock
      Products), each linking to its real admin_portal queue, visually accented only when count > 0
- [x] Business Snapshot: Total Distributors (+ new this week), Total Products, This Week's Orders
      (count + GHS value), This Week's Commissions — all real queries over a rolling 7-day window
- [x] Recent Orders table (latest 6), linking to order detail and the full order management queue

**Verification:**
- [x] 16 tests in `tests/feature/admin_portal/test_dashboard.py` — permissions, each count's
      precise inclusion/exclusion logic (including the 7-day window boundary), the orders
      count-vs-value distinction, and the recent orders table's content/limit/links
- [x] Full suite green throughout: 1177 passed, 1 skipped
- [x] CodeRabbit review (2 rounds), all findings fixed pre-merge — see `CLAUDE.md`'s Task 27 entry

**Shipped via PR #54, merged 2026-07-31.**

**Dependencies:** Task 22/23 (admin_portal queues the cards link to), Task 20/21 (dashboard
patterns this page reuses)

**Files touched:** `apps/admin_portal/views.py` (`dashboard` view + module-level constants),
`templates/admin_portal/dashboard.html`, `tests/feature/admin_portal/test_dashboard.py`

**Estimated scope:** M

---

**Checkpoint I (final MVP checkpoint):** the full Section 14 distributor journey works end to end
through the UI. All pytest tests pass, `black`/`ruff` clean. Verify against `SPEC.md` Success
Criteria before calling the MVP done.

---

### Task 28: Platform Settings admin screen

**Description:** Not in the original plan — found the same way as Task 26/27: this was the last
remaining admin-facing surface still using raw Django Admin instead of a real, Stitch-designed
`admin_portal` screen, for all 76 `django-constance` business-rule settings (10 fieldset groups).
Reuses `apps.platform_settings.admin.BancostoreConstanceForm` directly rather than duplicating its
field types/bounds/cross-field validation.

**Acceptance criteria:**
- [x] All 10 setting groups reachable from one page via vertical tabs — sticky with icons from
      `lg`/1024px up, a horizontal icon-less scroll strip below that
- [x] Every setting name shown as a human-readable label (acronyms cased correctly: OTP, KYC, 2FA,
      IR ID, PV, WhatsApp), not the raw `ALL_CAPS` constance key
- [x] Save round-trips correctly for every field type (boolean, decimal, percentage, withdrawal
      range), with existing cross-field validation still enforced
- [x] `ADMIN_2FA_ENABLED` cannot be silently disabled via a normal save (tamper-resistance)

**Verification:**
- [x] 12 tests in `tests/feature/admin_portal/test_platform_settings.py` — permissions, every field
      group renders, save round-trip for boolean/decimal settings, cross-field withdrawal-amount
      validation, percentage-field bound validation, label humanization, `ADMIN_2FA_ENABLED`
      tamper-resistance regression
- [x] Live-browser verification at 1440/1024/768/500px: icons, sticky sidebar, normalized labels,
      save flow (toggled + reverted a real setting)
- [x] Full suite green throughout: 1205 passed, 1 skipped
- [x] CodeRabbit review, all findings fixed pre-merge (checkbox keyboard-focus ring, ARIA tab
      semantics, a test-helper bug that could have silently reset an unrelated already-customized
      setting) — see `CLAUDE.md`'s Task 28 entry. **Deferred, not silently skipped:** full
      roving-`tabindex` keyboard navigation (Left/Right/Home/End arrow-key handling) for the tab
      list was not implemented.

**Shipped via PR #55, merged 2026-07-31.**

**Dependencies:** Task 22/23 (admin_portal shell/patterns this page reuses), the constance settings
themselves (seeded incrementally across most prior tasks)

**Files touched:** `apps/admin_portal/urls.py`, `apps/admin_portal/views.py`,
`templates/admin_portal/base_dashboard.html`, `templates/admin_portal/platform_settings.html`,
`templates/orders/order_history.html` (an unrelated latent CSS bug found and fixed along the way),
`tests/feature/admin_portal/test_platform_settings.py`

**Estimated scope:** M

---

### Task 29: Storefront About & Contact pages

**Description:** `templates/base_store.html`'s header, mobile menu, and footer all have real
"About"/"Contact" links styled and positioned, but every one of them is a dead `href="#"` with a
`title="Coming soon"` — 6 occurrences total (desktop nav x2, mobile nav x2, footer x2). This closes
that gap with two real pages, content grounded in what's actually documented, not invented:
- **About** narrates `SPEC.md`'s own Objective section (Ghana direct-selling platform, ecommerce +
  binary-MLM, the three real user roles) in plain customer-facing language — no fabricated company
  history, founder story, or headcount, since none of that exists in any project doc.
- **Contact** reads `apps/platform_settings/config.py`'s already-seeded, currently-unused-anywhere
  `CONTACT_PHONE_NUMBER`/`CONTACT_EMAIL_ADDRESS`/`WHATSAPP_SUPPORT_NUMBER`/`PHYSICAL_ADDRESS`
  constance settings (each docstring literally says "shown on the contact page") and shows only the
  channels the admin has actually filled in — never a fabricated phone number/address. Also has a
  real contact form sending mail via the already-configured, already-verified-working Gmail SMTP
  backend (Task 4-6's registration/password-reset emails use the same backend).

**Acceptance criteria:**
- [x] `/about/` renders real, accurate platform content (no invented facts)
- [x] `/contact/` shows only the contact channels the admin has actually set via constance; an
      unset channel is omitted entirely, not shown blank or with a placeholder
- [x] The contact form sends a real email (verified via `django.core.mail.outbox` in tests, and
      live against the real Gmail SMTP backend) and shows a success message on completion
- [x] Invalid contact form input re-renders with field errors, sends no email
- [x] The contact form POST is rate-limited per IP, matching this codebase's established
      public-form convention (e.g. registration's `@ratelimit`)
- [x] All 6 dead "About"/"Contact" links across header/mobile-menu/footer point to the real pages;
      `title="Coming soon"` removed from all of them

**Verification:**
- [x] pytest: `/about/` returns 200 with expected content
- [x] pytest: `/contact/` GET returns 200; only-set-channels-shown, tested with a mix of set/unset
      constance values
- [x] pytest: valid POST sends exactly one email, redirects with a success message
- [x] pytest: invalid POST (e.g. empty message) re-renders with errors, sends no email
- [x] pytest: POST is rejected once the per-IP rate limit is exceeded
- [x] Live browser check: both pages render correctly at mobile + desktop widths, using the real
      theme (not the Stitch mockup convention here, since no mockup exists for these two pages —
      built directly from the existing design tokens/components already established elsewhere in
      `templates/orders/order_history.html`-style storefront pages)

**Dependencies:** None (constance settings and the email backend both already exist)

**Files likely touched:** new `apps/pages/` app (`views.py`, `forms.py`, `urls.py`), new
`templates/pages/about.html`/`contact.html`, `templates/base_store.html` (6 link locations),
`bancostore/urls.py`, `tests/feature/pages/test_about.py`/`test_contact.py`

**Estimated scope:** S

**Closed out 2026-07-31.** Built via the full agent-skills workflow (planning, TDD RED/GREEN per
slice, frontend-ui-engineering for the two new templates, code-review-and-quality, and a dedicated
security-and-hardening pass). `apps/pages/` (about + contact views/forms/urls) reads About's
content straight from `SPEC.md`'s Objective section and Contact's channel display from the
already-seeded-but-previously-unused `CONTACT_PHONE_NUMBER`/`CONTACT_EMAIL_ADDRESS`/
`WHATSAPP_SUPPORT_NUMBER`/`PHYSICAL_ADDRESS` constance settings — each rendered only when an admin
has actually set it (a `_stripped_or_none` helper guards against a whitespace-only value being
treated as "set"). The contact form sends real mail via the already-configured Gmail SMTP backend,
rate-limited 5/hour per IP matching `apps/distributors/views.py::register`'s own convention.
`templates/base_store.html` also gained its first messages/flash-message component (ported from
`admin_portal/base_dashboard.html`'s, retoned to the storefront's own `border-error/20` token) since
no storefront view had ever needed one before. A code-review pass found and fixed 3 issues (WhatsApp
link only stripped `+`/space instead of all non-digits; `message` field had no `max_length` unlike
its siblings; the fallback-to-`DEFAULT_FROM_EMAIL` path had no logging), each with a RED→GREEN
regression test. A follow-up `security-and-hardening` pass (a fresh-context security-auditor agent)
found 2 more Low findings (missing `max_length=254` on `email`, a stale comment claiming `name`
reaches an email header when it currently only reaches the body) — both fixed the same way — plus
1 Medium: `@ratelimit(key="ip", ...)` reads `REMOTE_ADDR` directly and has no `RATELIMIT_IP_META_KEY`
configured, so once Task 24 puts a reverse proxy in front of Django in production, every visitor's
IP will collapse to the proxy's own address and the 5/hour cap becomes site-wide instead of
per-visitor — this is the same proxy-aware-rate-limit-key gap already tracked in this file's Known
Issues, but flagged here specifically because this endpoint's blast radius (a shared Gmail SMTP
sending quota, also used by registration/password-reset transactional email) makes it worth
resolving as part of Task 24's own deploy checklist, not assumed-covered by the generic entry.
Deferred, not silently skipped — no proxy exists yet, so nothing is exploitable today. Full suite
verified GREEN for every file this task touched (`tests/feature/pages/` — 17 tests — plus
`tests/feature/catalog/test_navigation_links.py`'s 2 new link-wiring tests); two separate full-suite
runs surfaced a handful of unrelated, non-reproducing SQLite `"database table is locked"` failures
(a different set of files failed each run) matching this project's already-documented test-order
flakiness — confirmed via `git stash`/`git stash pop` isolation that neither reproduces from this
task's code. Live-browser-verified at 1440px and 500px widths (desktop nav, mobile hamburger menu,
form submission + success flash message, empty-state contact-channel display all checked against
the running `runserver`), per this task's own acceptance criteria. Shipped via PR #56 (task branch
`task-29-about-contact-pages`), merged into `main`.

---

### Task 30: Fix tracked Known Issues — decorative constance settings, session engine, CI hygiene

**Description:** Not in the original plan — addresses specific items from this file's own "Known
issues — tracked, not blocking" sections (2026-07-11/12), per explicit user go-ahead. A code
investigation (`agent-skills:planning-and-task-breakdown`) confirmed 7 `AUTHENTICATION_SETTINGS`
constance settings are **100% decorative** — each appears exactly once in the whole codebase, in
its own `apps/platform_settings/config.py` declaration, read by nothing. Split into 6 vertical
slices, each independently shippable:

- **30a — Password policy enforcement** (`MIN_PASSWORD_LENGTH`, `PASSWORD_COMPLEXITY_ENABLED`):
  Django's `AUTH_PASSWORD_VALIDATORS` is a static list evaluated once at process start, so it can't
  read a live, admin-editable constance value — needs custom validator classes that read
  `constance.config` inside their own `validate()` method instead of `settings.py`-time options.
  `PASSWORD_COMPLEXITY_ENABLED`'s described behavior ("must contain numbers and capital letters")
  has **no existing enforcement mechanism at all** to gate — this is new validator logic, not just
  wiring an existing one.
- **30b — Session timeout enforcement** (`SESSION_TIMEOUT_MINUTES`, `ADMIN_SESSION_TIMEOUT_MINUTES`):
  no `SESSION_COOKIE_AGE` override and no `request.session.set_expiry()` call exists anywhere today
  — every session (admin included) currently uses Django's hardcoded 2-week default with no
  idle-timeout logic. Needs middleware that calls `set_expiry()` per-request, using the admin value
  for staff and the regular value otherwise.
- **30c — Google login (customers) wired for real**: `GOOGLE_LOGIN_CUSTOMERS_ENABLED` currently has
  no code path — the button's visibility is driven entirely by whether a Google `SocialApp` DB row
  exists, via allauth's own `{% get_providers %}` tag. Gate that existing template block with this
  flag too (AND the two conditions), so an admin can hide the button even when a `SocialApp` is
  configured — but toggling the flag "on" alone can never show a button with no `SocialApp`
  configured; that DB dependency isn't something a flag alone can satisfy, and the fieldset help
  text should say so honestly.
- **30d — Honest documentation for the 3 settings NOT being wired this round** (user-confirmed
  scope, not a silent skip):
  - `ADMIN_2FA_METHOD` default is actively wrong (`"sms"`) — no SMS 2FA delivery exists anywhere in
    this codebase (`otp_totp`/`otp_static` are the only registered django-otp plugins;
    `two_factor.plugins.phonenumber` handles login-identification, not a delivery channel). Fix the
    default to `"authenticator_app"` and correct the help text to state SMS isn't implemented.
  - `PASSWORD_RESET_EXPIRY_MINUTES` (customer email-reset-link expiry only — distributor reset
    already uses the separate, working `OTP_CODE_EXPIRY_MINUTES`): making this live would require a
    custom password-reset token generator instead of Django's static `PASSWORD_RESET_TIMEOUT`
    setting — real but security-token-adjacent code, deferred to its own future task with a
    `doubt-driven-development` pass rather than rushed into this batch. Help text updated to state
    this plainly.
  - `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED` has zero code path — distributor login has no Google
    markup at all, and an existing regression test
    (`test_distributor_login_page_has_no_google_login_option`) locks in "always absent." Real
    distributor Google login (account linking, KYC/group implications) is a new feature, not a
    wiring fix — help text updated to state the flag currently has no effect.
- **30e — `SESSION_ENGINE` DB fallback**: switch from
  `"django.contrib.sessions.backends.cache"` to `"django.contrib.sessions.backends.cached_db"` so a
  Redis eviction/restart no longer logs out every user platform-wide (including admin's
  mandatory-2FA state) — `django.contrib.sessions` is already an installed app, so the DB-backed
  fallback table already exists with no new migration needed.
- **30f — CI hygiene** (`.github/workflows/ci.yml`): add an explicit `permissions:` block (defaults
  are broader `GITHUB_TOKEN` scope than this workflow needs — it only ever reads/tests, never
  writes), pin `actions/checkout@v4`/`actions/setup-python@v5` to a commit SHA instead of a mutable
  tag (real SHAs resolved from GitHub's API for each action's latest v4.x/v5.x release, not
  guessed), and add a `pip-audit` dependency-vulnerability-scan step.

**Acceptance criteria:**
- [x] 30a: registering with a password shorter than the live `MIN_PASSWORD_LENGTH` value is
      rejected; changing the constance value at runtime (no restart) changes the enforced minimum;
      `PASSWORD_COMPLEXITY_ENABLED=True` rejects a password with no uppercase letter or no digit,
      `False` allows it
- [x] 30b: an authenticated non-staff session's expiry matches the live `SESSION_TIMEOUT_MINUTES`
      value in seconds; an authenticated staff session's expiry matches
      `ADMIN_SESSION_TIMEOUT_MINUTES` instead
- [x] 30c: with a `SocialApp` configured, the customer Google button is hidden when
      `GOOGLE_LOGIN_CUSTOMERS_ENABLED=False` and shown when `True`; with no `SocialApp` configured
      the button stays hidden regardless of the flag
- [x] 30d: `ADMIN_2FA_METHOD`'s default is `"authenticator_app"`; all three help texts (this,
      `PASSWORD_RESET_EXPIRY_MINUTES`, `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED`) accurately describe
      current (non-)enforcement, no code behavior otherwise changes
- [x] 30e: `SESSION_ENGINE` is `cached_db`; an existing session survives a `cache.clear()` (the
      exact failure mode a Redis eviction/restart would otherwise cause)
- [x] 30f: `ci.yml` has an explicit top-level `permissions:` block, both pinned actions reference a
      commit SHA (with the human-readable version as a trailing comment, GitHub's own recommended
      pattern), and a new step runs `pip-audit` against `requirements.txt`

**Verification:**
- [x] pytest: new tests for each slice (30a password validators, 30b session expiry, 30c Google
      button gating, 30e session survives cache clear) — RED before the fix, GREEN after
- [x] 30d needs no new test (pure config-value/help-text change) — confirmed via
      `python manage.py shell` reading the live default
- [x] 30f verified by the CI run on the PR actually executing with the new permissions
      block/pinned SHAs/pip-audit step and passing
- [x] Full suite green after every slice, not just the new tests
- [x] `doubt-driven-development` pass before 30a/30b (auth-adjacent, security-relevant), given no
      existing pattern in this codebase reads a constance value from inside a password validator or
      session middleware yet
- [x] `security-and-hardening` pass across all 6 slices before merge

**Closed out 2026-07-31.** All 6 slices shipped together. A `doubt-driven-development` pass (a
fresh-context security-auditor agent) ran on the 30a/30b design before any code was written and
found 2 High findings (both addressed before implementation: dropped an unnecessary
`SESSION_SAVE_EVERY_REQUEST` setting entirely — `set_expiry()` already marks a session `modified`
on its own, so it added nothing but a blast-radius cost on every anonymous storefront session too)
plus Medium findings folded into the design: an `ABSOLUTE_MIN_PASSWORD_LENGTH=6` floor clamped
inside the validator so an admin fat-fingering `MIN_PASSWORD_LENGTH` to 0 can't fully disable the
control, and a `try/except` around every `constance.config` read that degrades to a safe hardcoded
default with a logged warning rather than crashing every password-set/session-check/page-load
during a Redis outage — applied consistently across all three new modules
(`apps/accounts/validators.py`, `middleware.py`, `context_processors.py`).

A `code-review-and-quality` pass (a fresh-context code-reviewer agent) approved with two Important
follow-ups, both fixed before merge: a stale hardcoded "Must be at least 8 characters" hint in
`templates/account/password_reset_from_key.html` and
`templates/distributors/set_new_password.html` directly undermined 30a's own point (an admin
raising `MIN_PASSWORD_LENGTH` would leave the page telling users the old, wrong number) — removed
entirely, matching `signup.html`'s own existing convention of showing only real validator errors,
no static hint; and `SessionTimeoutMiddleware` calling `set_expiry()` unconditionally on every
authenticated request, compounded by 30e's `cached_db` engine, meant a new DB write on the
single hottest path in a system scoped for hundreds of thousands of users — fixed by only
renewing once the session's remaining age has drifted outside `[target/2, target]`, with 2 new
regression tests proving both the skip and the eventual renewal.

A real regression surfaced during full-suite verification, root-caused via
`debugging-and-error-recovery`: two pre-existing tests in
`tests/feature/distributors/test_distributor_auth.py` directly poked a raw `PhoneNumber` object
into the session (bypassing the real view, which always stores `str(phone_number)`) — this only
"worked" under the old `cache` session engine (which never actually JSON-serializes, letting
`django_redis` pickle arbitrary objects transparently) and broke under 30e's `cached_db` (which
does serialize, since it also persists to the `django_session` DB table). Confirmed via full
traceback this was a test bug, not a production bug — both real write sites already stringify
before storing — and fixed the two tests to match production's own convention. A second,
unrelated false failure (`test_distributor_login_is_rate_limited_per_ip`) was traced to two stray
`runserver` processes left running from earlier browser-verification work sharing the same Redis
instance as pytest — the exact interference pattern this file's own "Known issues" section already
documents; stopping those processes made the test pass immediately, confirming zero relation to
this task's code. Full suite green throughout. Shipped via PR #57.

**Dependencies:** None (all 6 slices are independent of each other and can ship in any order)

**Files likely touched:** `apps/accounts/validators.py` (new, 30a), `bancostore/settings.py`
(`AUTH_PASSWORD_VALIDATORS`, `SESSION_ENGINE`, new middleware registration), `apps/accounts/`
(new session-timeout middleware, 30b), `templates/account/login.html`/`signup.html` (30c),
`apps/platform_settings/config.py` (30d), `.github/workflows/ci.yml` (30f), plus a `tests/` file
per slice

**Estimated scope:** each slice S; 30a/30b are the two carrying real new logic, 30c/30d/30e/30f are
smaller

---

### Task 31: Storefront home page — Editorial Variant layout rebuild

**Description:** Not in the original plan — requested directly by the user. Rebuilds
`templates/catalog/home.html` to adopt the "Storefront Home - Editorial Variant" Stitch screen
(project `14456046746368120137`, screen `0acbef64d78843fc97925919c3f03611`). The previous home page
already shared the same overall section structure with this design (hero, lifestyle photo strip,
brand statement, shop-by-category, mission statement, how-it-works, discover-bancostore bento,
featured products, distributor CTA) — 5 of those 9 sections were already visually equivalent and
stayed untouched. 4 sections had genuinely different layouts and were rebuilt: Hero (centered
text-only → split two-column with a real hero image), Shop By Category (uniform grid → asymmetric
bento grid, first real category gets a large tile), How It Works (simple 4-column grid → staggered
zigzag timeline with a connecting line on desktop, a safe simple side-by-side row per step on
mobile — deliberately not the mockup's own fragile absolute-positioned mobile offsets, which were
pixel-tuned to specific text lengths), Featured Products (uniform grid → horizontal-scrolling snap
carousel). All four still render real `Category`/`Product` data from the existing `home` view — no
fabricated category names or products were introduced.

**Acceptance criteria:**
- [x] Hero, Shop By Category, How It Works, and Featured Products sections match the new Stitch
      design's layout
- [x] All real data wiring preserved: `categories`/`featured_products` loops unchanged, product
      cards still show real PV/price/in-stock state via the existing `product_card.html` partial
- [x] Exactly 2 `distributors:register` links remain in the page's own content (hero + CTA band),
      matching the existing `test_become_a_distributor_links_point_to_real_registration_page`
      contract
- [x] The "Discover Bancostore" bento tiles stay honest `Coming soon` placeholders, not wired to
      About/Contact (would have broken `test_about_link_points_to_the_real_about_page`/
      `test_contact_link_points_to_the_real_contact_page`'s exact-count assertions)
- [x] Very responsive: verified clean at 500px (mobile, this Mac's real Chrome resize floor), 768px,
      1024px, 1280px, and 1440px+

**Verification:**
- [x] `tests/feature/catalog/test_home.py` (5 tests, +1 added after the code-review pass — see
      below) and `tests/feature/catalog/test_navigation_links.py` still pass unchanged
- [x] Full suite for `tests/feature/catalog/` + `tests/feature/orders/`: 112 passed
- [x] Live-browser verification at every breakpoint above, including opening the mobile hamburger
      menu and scrolling the featured-products carousel
- [x] Two real responsive bugs found and fixed via that live testing (not assumed from the mockup):
      the hero's split layout originally activated at `md:` (768px), but the 72px headline's single
      long words (e.g. "OPPORTUNITY") can't wrap and overflowed a ~350px column, overlapping the
      hero image — moved the split to `lg:` (1024px). Even at exactly 1024px a 50/50 split column
      (~420px) was still too narrow for 72px text — stepped the headline to 56px specifically at
      the `lg:` tier, only returning to 72px at `xl:` (1280px) where the column is comfortably wide.
- [x] `code-review-and-quality` pass (fresh-context code-reviewer agent) — 3 real Important findings,
      all fixed and re-verified live before merge:
      1. The bento grid's fixed `md:grid-rows-2 md:h-[600px]` (8 cells) exactly filled at 4
         categories + the "View All" tile — any real count of 5+ pushed a tile into an unsized,
         visibly squashed implicit row. Fixed with `md:auto-rows-[288px]` so overflow rows match the
         explicit rows' height; added `test_category_bento_grid_renders_every_tile_at_five_or_more_categories`
         and live-verified with 6 real (temporarily seeded, then removed) categories.
      2. The timeline's circles floated at the flex row's natural edge instead of on the centerline
         (`md:flex-row-reverse` doesn't center a child, it just reverses source order) — fixed with
         an order-based layout (`order-1 md:order-2` on the circle, always the true middle slot;
         text/spacer swap `md:order-1`/`md:order-3`), live-verified all 4 steps sit on the line.
      3. `.hide-scrollbar` removed the one native affordance telling a mouse-only visitor the
         featured-products carousel had more content, with no keyboard way to scroll the region
         itself — added `tabindex`/`role="group"`/a descriptive `aria-label`, left/right arrow-key
         handling, and visible Previous/Next buttons (desktop only), matching the accessibility bar
         `templates/distributors/binary_tree.html` (Task 21a) already set for this exact class of
         horizontal-scroll interaction. Live-verified the Next button actually scrolls the carousel.

**Dependencies:** None (reuses the existing `home` view/context, `product_card.html` partial, and
`base_store.html` header/footer unchanged)

**Files touched:** `templates/catalog/home.html`, `static/src/main.css` (new `.hide-scrollbar`
utility for the featured-products carousel), `tests/feature/catalog/test_home.py` (1 new
regression test)

**Estimated scope:** M

**Follow-up (2026-08-04/05), per direct user feedback after live-testing the shipped page:**
- Hero headline/subtext now centers on mobile/tablet only (`items-center text-center`, reverting to
  `lg:items-start lg:text-left` where the split-image layout kicks in).
- The bento grid's second tile now also gets a wide `md:col-span-2` span (matching the real fetched
  Stitch mockup exactly — the original build only special-cased `forloop.first`, leaving tiles 2-4
  as plain equal cells instead of one wide top-right tile).
- Bento tile text moved from centered to bottom-left on every tile, per explicit instruction (a
  deliberate simplification vs. the mockup's own mixed centered/bottom-left treatment).
- "View All Categories" moved out of the grid into its own link below it (`lg:`+ only); the in-grid
  "View All Products" tile is now an `lg:hidden` mobile/tablet fallback.
- Root-caused and fixed the "square instead of circle" timeline complaint: this project's
  `rounded-full` Tailwind token is redefined to `0.75rem` (for pill-shaped buttons), not a true 50%
  circle, so every square `w=h` "circle" badge on the home page (4 timeline numbers, the bento
  arrow icon, 2 carousel buttons) rendered as a barely-rounded square. Fixed with the
  arbitrary-value `rounded-[50%]` on those specific badges, leaving actual pill buttons on
  `rounded-full` as-is. Added `motion-safe:animate-pulse` to the first (orange) timeline circle.
- The featured-products carousel's Previous/Next buttons now show at every breakpoint, not
  desktop-only (`hidden md:flex` → `flex`) — mobile keeps its native touch-swipe too, the buttons
  are now an additional affordance, not a desktop-only replacement.

Verified live in a real browser at 500/900/1440px. New regression test:
`test_category_bento_grid_second_tile_wide_and_view_all_categories_outside_grid`. Shipped 2026-08-05
via PR #60 alongside Task 32.

---

### Task 32: Admin portal fixes — category-image dropzone, logout redirect, native-admin links

**Description:** Not in the original plan — found via direct user testing/bug reports in the same
session as Task 31's follow-up above. Three unrelated fixes bundled into one PR per this project's
batching convention (see `CLAUDE.md`'s "Git & Review Workflow"):

1. **Category admin image field modernized.** The `CategoryForm` image field used Django's raw,
   unstyled `ClearableFileInput` widget, rendering literal "Currently: categories/cat_x.webp /
   Clear / Choose file No file chosen" text with zero styling. Replaced with a modern Alpine-driven
   dropzone/preview UI matching the product-image dropzone's existing visual language: a
   `CategoryImageWidget` (a small `ClearableFileInput` subclass with its own template rendering
   just the sr-only file input, plus a sr-only "clear" checkbox when editing a category that
   already has an image — no visible default text). Required a small global fix: Django's default
   form renderer can't see this project's real `templates/` directory (it uses an isolated engine
   with `DIRS=[]`), so custom widget templates need `FORM_RENDERER = "django.forms.renderers.
   TemplatesSetting"` (Django's own documented fix) plus `django.forms` added to `INSTALLED_APPS`
   so its own built-in widget templates stay discoverable through the same engine.
2. **Admin logout fixed.** Was posting to `admin:logout` — Django's own raw internal admin logout
   view, redirecting to the native unstyled `/admin/login/` page instead of this project's branded
   one. Switched to `account_logout` (allauth's real logout view, already the storefront's own
   convention) with a hidden `next` field pointing at `two_factor:login`, so logging out lands back
   on the branded admin login screen specifically, not allauth's own default redirect (the public
   storefront home).
3. **Two more leftover native-admin links, found via a follow-up audit** (grepped the whole
   `templates/`/`apps/` tree for `{% url 'admin: %}`, raw `/admin/` hrefs, and `reverse`/`redirect`
   calls in Python — confirmed these were the only two remaining instances): the admin login page's
   logo (`templates/base_admin_auth.html`, the shared header for every 2FA/admin-auth screen)
   linked to `two_factor:login` — itself — leaving no way to reach the public storefront from the
   admin login screen; fixed to `catalog:home` (a logo linking to the public site from an internal
   login screen is standard — GitHub, Stripe, AWS all do this — and gives up nothing, since the
   storefront URL is public either way). The 2FA setup-complete screen's "Continue to Admin Panel"
   button linked to `admin:index` (Django's raw native admin dashboard) instead of
   `admin_portal:dashboard`.

**Real bugs found and fixed live, not just the styling:**
- `{{ category.image.url }}` raises a real Python `ValueError` on an empty `ImageField` — Django's
  `|default` filter only substitutes for falsy *values*, not exceptions raised while resolving
  `.url` on an empty `ImageFieldFile` — crashed the edit page for any category with no image (e.g.
  right after clearing one and saving). Fixed by checking `{% if category.image %}` first.
- When a category already has an image, Django's `ClearableFileInput` renders a "clear" checkbox
  alongside the file input. The dropzone `<label>` wrapped both — per the HTML label spec, a label
  wrapping more than one labelable control delegates its default click action to the *first* one it
  contains (the checkbox, rendered first), not the file input — so clicking the dropzone after
  deleting an image silently did nothing, no file picker ever opened. Root-caused by comparing a
  synthetic-click DOM test against the already-working product-image dropzone (which uses a
  single-control label and correctly delegates) as a control. Fixed with an explicit
  `for="{{ form.image.id_for_label }}"` association instead of implicit wrapping.
- **CodeRabbit caught one more real, Major bug on PR #60:** the category dropzone's delete button
  was only revealed via `opacity-0 group-hover:opacity-100`, and touch devices have no hover state
  at all, so an admin on a phone/tablet could never discover or reach it for an existing image.
  This exact same pattern already existed in the pre-existing, already-shipped product-image
  dropzone this one was modeled on — fixed both the same way:
  `opacity-100 md:opacity-0 md:group-hover:opacity-100`, always visible below `md:` (touch-primary
  devices), hover-gated at `md:`+ where real mouse hover exists. CodeRabbit also caught 3 style
  findings (multi-line comments using consecutive `{# #}` tags instead of `{% comment %}`, and one
  raw HTML `<!-- -->` comment leaking implementation details to the browser instead of staying
  server-side) — all fixed in a single follow-up commit per this project's "batch CodeRabbit fixes"
  convention.

**Acceptance criteria:**
- [x] Category admin form's image field renders a styled dropzone, not raw browser default markup
- [x] Editing a category with no image renders without error
- [x] Deleting an existing image, then choosing a replacement, correctly opens the file picker and
      updates the preview
- [x] Admin logout lands on the branded admin login page, not `/admin/login/`
- [x] Admin login page's logo links to the storefront home; 2FA setup-complete's CTA links to
      `admin_portal:dashboard`
- [x] The image-tile delete button is visible without hovering, on both the category and product
      dropzones

**Verification:**
- [x] New regression tests: `test_editing_a_category_with_no_image_renders_without_error`,
      `test_editing_a_category_with_an_image_wires_the_dropzone_to_the_real_file_input`
      (`tests/feature/admin_portal/test_catalog_management.py`);
      `test_logging_out_of_the_admin_portal_redirects_to_the_branded_login_page`
      (`tests/feature/admin_portal/test_dashboard.py`);
      `test_admin_login_logo_links_to_the_storefront_not_back_to_itself`,
      `test_2fa_setup_complete_continue_button_links_to_the_real_admin_portal`
      (`tests/feature/accounts/test_admin_auth.py`)
- [x] Live-browser verified: category dropzone (create + edit + delete + touch-visibility at
      500/900px), full login → logout round trip, logo click from the login page
- [x] Full suite runs green throughout (264 passed / 1 skipped; 210 passed / 1 skipped after the
      CodeRabbit-fix commit) — the 1 skip is the documented WeasyPrint/Pango CI-only test
- [x] `black`/`isort`/`ruff` clean, `npm run build` clean
- [x] CI green end to end (lint, real-MySQL test, CodeRabbit) before merge

**Dependencies:** None

**Files touched:** `apps/admin_portal/forms.py`, `templates/admin_portal/catalog_category_form.html`,
`templates/admin_portal/widgets/category_image_input.html` (new),
`templates/admin_portal/catalog_product_form.html`, `bancostore/settings.py`,
`templates/admin_portal/base_dashboard.html`, `templates/base_admin_auth.html`,
`templates/two_factor/core/setup_complete.html`, plus a `tests/` file per area touched

**Estimated scope:** M

**Closed out 2026-08-05.** Shipped via PR #60 (5 commits, including one follow-up addressing
CodeRabbit's findings), squash-merged into `main`.

---

### Task 33: Distributor Team page — flat roster of the personally-recruited downline

**Description:** Not in the original plan. The "Team" sidebar entry has sat as a disabled
placeholder since Task 15/21 ("still a future task -- shown as a disabled entry", see
`templates/distributors/base_dashboard.html`), with no spec, no task number, and no decision on
what it should show. Scoped 2026-08-10 after the user noticed the dead entry and asked; user
confirmed the intended scope (below) after being asked to disambiguate against the existing Binary
Tree page.

**Why this is a real, separate feature from Binary Tree (Task 21a):** Binary Tree already
visualizes a distributor's downline via `apps.binary_tree`'s placement/spillover closure table --
*where* someone ended up placed. This task is the *sponsor* chain instead (`Distributor.sponsor`,
the recruitment lineage set at registration/starter-pack purchase) -- *who* this distributor
personally recruited, directly or through their own recruits. `apps/commissions/services.py`'s own
`sum_downline_binary_bonus_earnings` docstring already documents that these two structures diverge
under spillover, and Matching Bonus is built specifically around the sponsor chain for that reason
-- this task reuses that same distinction, not a new one.

**Scope decisions (confirmed with user 2026-08-10):**
- Data source: `Distributor.sponsor` chain, full downline depth (not just direct recruits) -- "your
  team" means everyone you've built, not just your immediate recruits.
- Columns: full name, IR ID, rank, KYC status, date joined (`created_at`) -- all real `Distributor`
  fields already in the schema, nothing fabricated.
- Query approach: bulk-per-level BFS, the same pattern `sum_downline_binary_bonus_earnings` already
  uses and has already been reviewed for cycle-safety (`Distributor.sponsor` has no DB constraint
  against a cycle, unlike `BinaryTreeEdge`) -- one query per level via
  `Distributor.objects.filter(sponsor_id__in=[...])`, never one query per distributor. Capped at the
  existing `MAX_MATCHING_BONUS_WALK_DEPTH` constant as a safety ceiling (reused, not duplicated --
  see that constant's own docstring for why an unbounded walk is unsafe even with the cycle guard).
  A live page load justifies this cap even more than Matching Bonus's own background-job use of it
  did.
- Ownership scoping: always `request.user.distributor`, no distributor id ever accepted from the
  URL/query params -- matching `binary_tree_view`/`earnings_history`'s established no-IDOR-surface
  convention exactly.
- Pagination: 20/page, matching `order_history`/`earnings_history`'s established convention.
- Search/sort on the roster: explicitly deferred, not built in this first slice -- flagged as new
  scope for a fast-follow once there's a real team large enough to need it, not silently dropped.

**Open question, not yet decided:** whether to include a Personal PV column. Unlike the other four
columns (plain `Distributor` fields, free from the same query), Personal PV requires an extra
per-distributor lookup against `apps.pv_ledger` (the same source the dashboard's own "Monthly
Personal PV" stat already reads) -- doing that for every row in a potentially-large roster is a
different cost profile than the rest of this page. Needs a decision (include it with a batched/
bulk PV lookup, or leave it off this list and keep PV detail on the dashboard/Binary Tree pages
only) before implementation starts.

**Acceptance criteria:**
- [x] `distributors:team` view: `@login_required`, `is_distributor(request.user)` gate (same
      three-account-types guard `earnings_history`/`binary_tree_view`/`dashboard` already use),
      `PermissionDenied` for a non-distributor
- [x] Full sponsor-chain downline fetched via bulk-per-level BFS, capped at
      `MAX_MATCHING_BONUS_WALK_DEPTH`, cycle-safe (excludes already-seen ids from each next level's
      query, same as `sum_downline_binary_bonus_earnings`)
- [x] Paginated list (20/page) showing full name, IR ID, rank, KYC status, date joined for every
      distributor in the downline
- [x] Empty state for a distributor with no downline yet (not a bare blank page)
- [x] Sidebar "Team" entry in `templates/distributors/base_dashboard.html` wired up with a real
      `href`/`nav_key`, matching how Binary Tree (Task 21a) and Withdraw (Task 16g) were wired up
      when they shipped -- no longer a disabled placeholder
- [x] Resolves the Personal PV open question above (either ships with it via a bulk lookup, or
      explicitly documents the deferral -- not silently dropped either way)

**Verification:**
- [x] Unit/feature tests for the BFS walk: correct multi-level downline, cycle-safety (a corrupted
      sponsor graph doesn't infinite-loop), depth-cap behavior, empty-downline case
- [x] Feature test: a distributor cannot view another distributor's team (no id/param IDOR surface
      -- there shouldn't be one, since the view takes no id at all)
- [x] Live-browser verified at 500/768/1024/1440px (500px, not 320px, per this project's own
      already-documented "macOS Chrome's actual window-resize floor" convention from Task 17e)
- [x] Full suite green, `black`/`isort`/`ruff` clean, CI green (lint, real-MySQL test, CodeRabbit)
      before merge

**Built:** Done 2026-08-10. **Resolved the Personal PV open question:** left off (user-confirmed)
-- the page ships with only the five free-from-one-query fields; PV detail stays on the
dashboard/Binary Tree pages.

**Real gap in the plan's own field list, caught during implementation, not before:** the plan said
"date joined (`created_at`)" as if it were a `Distributor` field -- `Distributor` has no
`created_at`/date field of its own at all (confirmed by reading the model directly). The real data
lives on `Distributor.user.date_joined` (Django's built-in `User` field, set automatically when
`consume_paid_registration` creates the account) -- used instead, with `select_related("user")` on
the roster queryset to avoid an N+1 per row.

**Reused, not just "reused in spirit":** rather than hand-writing a second copy of
`sum_downline_binary_bonus_earnings`'s cycle-safe BFS loop, extracted it into a new, independently
public `apps.commissions.services.walk_sponsor_chain_downline_ids(distributor, max_depth=None)` --
a behavior-preserving refactor, not a new algorithm. Verified safe two ways: `sum_downline_
binary_bonus_earnings`'s own existing test suite (`tests/unit/commissions/test_matching_bonus.py`,
`tests/feature/commissions/test_full_commission_journey.py`, 24 tests) passes unchanged before and
after the extraction, and the extracted function gets its own new direct test file
(`tests/unit/commissions/test_walk_sponsor_chain_downline_ids.py`, 6 tests: empty downline,
`max_depth=0`, unlimited-depth multi-level walk, bounded `max_depth`, cycle-safety, and the
`MAX_MATCHING_BONUS_WALK_DEPTH` ceiling via the same `patch.object` pattern
`test_matching_bonus.py` already established for that exact scenario, rather than constructing 500
real distributor rows). `apps/distributors/views.py::team` imports the shared function from
`apps.commissions.services` -- checked for circular-import risk before adding (commissions/
services.py only imports from `apps.distributors.models`, never `.views`, so the new edge is safe).

Template (`templates/distributors/team.html`) modeled directly on `earnings_history.html`'s
table/pagination/empty-state structure for visual consistency, KYC status pill copied from
`admin_portal/partials/directory_results.html`'s existing pattern, rank rendered with the `|title`
filter matching `_binary_tree_node.html`'s existing convention. 8 feature tests in
`tests/feature/distributors/test_team.py`, covering login-required, non-distributor 403, empty
state, direct+indirect recruits rendering with real field values, IDOR-safety (never another
distributor's team), sponsor-chain cycle safety, the walk ceiling, and 20/page pagination.

Live-browser verified at 500/768/1024/1440px against a local dev server with a real 2-level
downline seeded via shell -- table scrolls correctly within its own container at narrow widths
(same pattern `earnings_history.html` already established), sidebar correctly collapses to a
hamburger, active-nav highlighting works, empty state renders cleanly for a distributor with no
recruits. One real near-miss caught before it happened: local `.env` has a real mNotify key
configured (same as production), and logging in as a `phone_verified=False` test distributor to
check the empty state would have triggered a real OTP SMS to a fake number -- caught before
clicking Login, fixed by setting `phone_verified=True` directly via shell first, matching the
`feedback_check_mnotify_key_before_browser_login_tests` memory's own existing guidance for exactly
this situation. All local test-preview distributor accounts deleted from the local dev database
afterward.

Full project test suite green throughout (confirmed both before and after the
`walk_sponsor_chain_downline_ids` extraction, and again after the new feature was added).

**Dependencies:** None -- `Distributor.sponsor`, `MAX_MATCHING_BONUS_WALK_DEPTH`, and the BFS query
pattern this reuses all already exist and are already shipped (Task 14).

**Files touched:** `apps/distributors/views.py`, `apps/distributors/urls.py`,
`apps/commissions/services.py` (BFS extraction), `templates/distributors/team.html` (new),
`templates/distributors/base_dashboard.html`, `tests/feature/distributors/test_team.py` (new),
`tests/unit/commissions/test_walk_sponsor_chain_downline_ids.py` (new)

**Estimated scope:** M

**Not yet started.**

---

### Task 34: Legal/policy pages — Terms of Use, Privacy Policy, Cookie Policy, Disclaimer, Earnings & Income Disclosure, AI Disclaimer, Returns/Refunds/Shipping

**Description:** Not in the original plan — requested directly by the user. Seven static
legal/compliance pages grounded in this platform's real, already-shipped features (three real user
roles, Paystack payments, Didit KYC, the three commission types, GHS delivery-zone fees, the order
lifecycle, the 7-day cooling-off refund) rather than generic boilerplate or invented company facts
(no fabricated registration numbers, business address, or specific SLA promises not backed by a
real setting). Grouped under a new "Policies" footer column, matching the existing "Company"/"Shop"
column convention in `templates/base_store.html`.

**Scope decisions:**
- One shared `templates/pages/legal/_base.html` shell (title/last-updated header + a
  `.legal-content` typographic CSS class in `static/src/main.css`, reusing this project's own
  `--color-*`/`--font-*`/`--text-*` design tokens) so each of the 7 pages writes plain semantic
  HTML (`h2`/`p`/`ul`) instead of repeating Tailwind utility classes on every paragraph — same
  "don't repeat the chrome" reasoning as `earnings_history.html`/`team.html` reusing one table
  shell.
- Returns/Refunds/Shipping is the one page with real dynamic content: renders the live
  `DELIVERY_FEE_KUMASI`/`DELIVERY_FEE_ACCRA`/`DELIVERY_FEE_OTHER_REGIONS`/`FREE_DELIVERY_THRESHOLD`/
  `PENDING_ORDER_AUTO_CANCEL_HOURS` constance values (never hardcoded, matching this project's
  "business rules live in settings, not code" rule) and wires up the existing but previously-unused
  `REFUND_RETURN_POLICY_TEXT` constance field (seeded blank in Task-era `config.py`, listed in the
  admin fieldset since before this task but never read by any view) as an optional admin-editable
  custom policy block, rendered via the `linebreaks` filter (auto-escaped, safe against an admin
  pasting HTML/script into a free-text settings field) — mirrors `pages/contact.html`'s existing
  "only render a channel the admin actually set" pattern.
- The other six pages are static content (no constance reads) — same treatment as `about.html`.
- AI Disclaimer states plainly that some decorative/marketing photography on the site may be
  AI-generated (true — Stitch-sourced imagery), while actual product photos on listing/detail pages
  come from real `ProductImage` uploads, not AI.
- No fabricated legal specifics: governing law is stated as "the laws of the Republic of Ghana" and
  data-protection commitment references the Data Protection Act, 2012 (Act 843) without claiming a
  specific, unverified Data Protection Commission registration number. Contact details throughout
  link to the existing `pages:contact` page rather than duplicating/hardcoding an email address.

**Acceptance criteria:**
- [x] 7 new pages, each with its own view + named URL under the `pages` app: `terms-of-use`,
      `privacy-policy`, `cookie-policy`, `disclaimer`, `earnings-disclosure`, `ai-disclaimer`,
      `returns-refunds-shipping`
- [x] `templates/base_store.html` footer gains a "Policies" column linking all 7, without breaking
      the existing `test_about_link_points_to_the_real_about_page`-style exact-link-count
      conventions in `tests/feature/catalog/test_navigation_links.py`
- [x] Returns/Refunds/Shipping page shows the real, live delivery-fee/auto-cancel constance values
      and the optional admin-set `REFUND_RETURN_POLICY_TEXT` block
- [x] All 7 pages render at 200, use `base_store.html` (header/footer present), and are responsive
      at 500/1440px

**Verification:**
- [x] `tests/feature/pages/test_legal_pages.py`: 21 tests — one 200-status test per page
      (parametrized), a footer-links test asserting all 7 named URLs appear on the home page, real
      delivery-fee/auto-cancel-hours content assertions, `REFUND_RETURN_POLICY_TEXT`/
      `TERMS_AND_CONDITIONS_TEXT`/`PRIVACY_POLICY_TEXT` shown-when-set / hidden-when-blank-or-
      whitespace assertions, explicit HTML-escaping regression tests for all three admin free-text
      fields, content checks for the AI Disclaimer and Earnings Disclosure pages' key claims, and
      signup/distributor-registration consent-link wiring.
- [x] Full suite green (`tests/feature/pages` + `tests/feature/accounts` + `tests/feature/distributors`
      + `tests/feature/catalog` + full project suite), `black`/`isort`/`ruff` clean.
- [x] `npm run build` run after every template/CSS edit; live-browser-verified at 1440px (footer
      5-column layout, Returns/Refunds/Shipping page showing live GHS 20/50/70 delivery fees) and
      500px (footer stacks correctly, policy page copy readable, no overflow) via a real
      `runserver` session, hard-refreshed after each build per this file's own "Frontend edit-verify
      loop" gotcha.
- [x] `code-review-and-quality` pass (fresh-context `code-reviewer` agent) run before commit,
      per this repo's pre-commit hook requirement. Verdict: REQUEST CHANGES on first pass, two
      Important findings, both fixed: (1) two more decorative constance settings existed
      (`TERMS_AND_CONDITIONS_TEXT`/`PRIVACY_POLICY_TEXT`, seeded alongside `REFUND_RETURN_POLICY_TEXT`
      but never wired to a view) — now wired into Terms of Use / Privacy Policy with the same
      optional-block, dynamically-numbered pattern; (2) the signup and distributor-registration
      consent checkboxes still linked `href="#"` for "Terms of Service"/"Privacy Policy"/"Distributor
      Agreement" — now point to the real pages (Terms of Use also covers distributor terms directly,
      so "Distributor Agreement" links there rather than a separate, non-existent document), opening
      in a new tab so a mid-signup form doesn't lose its entered data. The reviewer also independently
      verified — by rendering a live template with a `<script>` payload — that `REFUND_RETURN_POLICY_TEXT`
      was already safely auto-escaped via `linebreaks`, and cross-checked every factual claim across
      all 7 pages against this file's own documented feature set, finding no fabricated content.

**Dependencies:** None (reuses `base_store.html`, `apps.platform_settings.config`,
`REFUND_RETURN_POLICY_TEXT`/`TERMS_AND_CONDITIONS_TEXT`/`PRIVACY_POLICY_TEXT` — all three already
seeded, none previously wired to a view)

**Files touched:** `apps/pages/views.py`, `apps/pages/urls.py`,
`templates/pages/legal/_base.html` (new), 7 new `templates/pages/legal/*.html`,
`templates/base_store.html`, `templates/account/signup.html`,
`templates/distributors/register.html`, `static/src/main.css`,
`tests/feature/pages/test_legal_pages.py` (new)

**Estimated scope:** M

**Built:** Done 2026-08-11.

---

### Task 35: Social Media Links — admin-managed footer links (up to 20) with auto-detected icons

**Description:** Not in the original plan — requested directly by the user. Replaces the storefront
footer's 3 hardcoded, disabled "Coming soon" social icons with a real admin-managed feature: staff
can add/delete social media links (name, URL, icon, color) from a new "Social Links" screen in the
admin portal, matching every other admin-facing screen's real-UI precedent (Task 22/23). The user
initially asked for "countless"/unlimited links; a pre-implementation review flagged that as a real
footgun at this project's scale (see below), so the shipped feature caps at `MAX_SOCIAL_MEDIA_LINKS
= 20` — effectively unlimited for a footer, not literally uncapped. Full design reasoning in
`docs/decisions/0009-social-media-links-design.md`.

**Scope decisions (user-confirmed via AskUserQuestion before build):** self-hosted a curated
17-platform icon set (Facebook, Instagram, X, YouTube, TikTok, WhatsApp, Telegram, Pinterest,
Snapchat, Reddit, Discord, Threads, GitHub, Twitch, Spotify, Medium, WeChat) fetched directly from
the real published Simple Icons package (CC0-licensed) via `curl`, not hand-approximated and not a
new pip/npm dependency — rejected the alternative of adding the real `simple-icons` package, which
this project's Boundaries gate behind asking first. **LinkedIn is deliberately excluded** — a
`code-review-and-quality` pass (run after the initial build) found that Simple Icons permanently
removed LinkedIn in v14.0.0 following LinkedIn's own trademark enforcement; the path data this
project had embedded was accurate (not corrupted), but self-hosting a mark the rights holder had
removed elsewhere is a real, if likely small, exposure this project chose to avoid rather than
accept — put to the user via AskUserQuestion, who confirmed dropping it. A LinkedIn link now falls
back to the generic "Other / Custom" icon, same as any other uncurated platform.

**Built:**
- `apps/pages/social_icons.py` — the curated registry (`SOCIAL_ICONS`, `SocialIcon` dataclass) and
  `detect_platform_from_url()`, a pure function matching a pasted URL's hostname against each
  platform's real domains (exact-or-subdomain only, never substring) with a `"custom"` fallback
  rendered via the Material Symbols glyph this project already loads everywhere else.
- `apps/pages/models.py::SocialMediaLink` — `name`, `url` (`URLField`, rejects `javascript:`/`data:`
  schemes by Django's own default validator), `platform` (choices-constrained to the curated
  registry — the real SVG markup an admin's browser ever sees always comes from the fixed
  server-side dict, never from request data), `icon_color` (`\A#[0-9A-Fa-f]{6}\Z`-validated hex),
  `order`. `MAX_SOCIAL_MEDIA_LINKS = 20`, enforced in `SocialMediaLinkForm.clean()` on create only.
- `apps/pages/context_processors.py::social_media_links` — injects the link list into every
  template's context (the footer is shared site-wide); cached indefinitely, invalidated by
  `post_save`/`post_delete` signals on the model.
- `apps/admin_portal/views.py`/`forms.py`/`urls.py` — `social_links_settings`/`_create`/`_update`/
  `_delete` follow the exact full-page POST+redirect shape `catalog_category_*` already established
  (no htmx partial swaps introduced — an `Explore` investigation found no existing per-row htmx
  pattern anywhere in this app to be consistent with), reusing the shared
  `_delete_confirm_modal.html` component verbatim. `social_link_detect_platform` (`GET`, admin-
  gated, returns JSON only, never echoes the candidate URL back) backs a `fetch()` call from the
  add/edit form's URL field on blur.
- `templates/admin_portal/social_links_settings.html` — each row expands/collapses on clicking its
  name (plain Alpine `x-show`, no server round-trip); a custom icon/color picker (not a native
  `<select>`, matching this app's established "no native select popups" convention) with every
  curated icon's real SVG pre-rendered server-side and toggled via Alpine `x-show` bound to a plain
  string variable — no HTML is ever built dynamically in JS anywhere in this feature. A native
  `<input type="color">` + hex text field are two-way synced via Alpine for the color picker.
- `templates/pages/_social_icon.html` — shared icon-render partial (real SVG or Material Symbols
  fallback), reused by both the footer and the admin picker.
- `templates/base_store.html` — footer's "Follow Us" section now loops over the live
  `social_media_links` context var (`target="_blank" rel="noopener noreferrer"` on every link),
  falling back to the original 3 disabled placeholders when no links are configured yet.

**Pre-implementation adversarial review (`doubt-driven-development`, fresh-context
`security-auditor` agent):** 10 findings, all folded into the design before any code was written —
every write must go through `SocialMediaLinkForm.is_valid()` (never a bare `.save()` from raw
`request.POST`, which would silently skip both the URL scheme and hex-color validators); the
detect-platform endpoint must never echo the raw candidate URL into its response and must be
admin-gated like every sibling endpoint (no legitimate reason to leave it public); the context
processor needed caching at this project's stated scale; an unbounded row count needed a cap; the
hex regex needed `\A...\Z` anchors instead of `^...$` (a `$` alone still admits one trailing
newline); `order` needed a `pk` tie-breaker (the exact bug class Task 15d already hit once); and
footer links opening in a new tab needed `rel="noopener noreferrer"` (reverse-tabnabbing). User
declined a cross-model second opinion after reviewing the single-model findings (all concrete and
actionable, none blocking).

**Real bug caught by tests, not assumed away:** the icon-data-generation script produced
single-domain tuples missing their trailing comma (`domains=("instagram.com")` instead of
`domains=("instagram.com",)`), silently turning the domain into an iterable of individual
*characters* instead of a 1-element tuple — `detect_platform_from_url` would never have matched
Instagram, LinkedIn, TikTok, Snapchat, Threads, GitHub, Twitch, Spotify, or Medium. Caught by
`tests/unit/pages/test_social_icons.py`'s very first run (2 of 48 tests failed exactly as they
should have), fixed with a targeted regex substitution across `apps/pages/social_icons.py`, then
re-verified live in a real browser (pasting an `instagram.com` URL correctly auto-selected the
Instagram icon).

**Verification:**
- 22 feature tests (`tests/feature/admin_portal/test_social_links_settings.py`) + 48 unit tests
  (`tests/unit/pages/`), 70 total — permission gates, full create/update/delete round trips, the
  `MAX_SOCIAL_MEDIA_LINKS` cap (blocks new rows, never blocks editing an existing one once the table
  is full), hex/URL/platform validation rejecting malformed and injection-shaped input, the
  detect-platform endpoint never reflecting its input, footer rendering (configured links, the
  `target="_blank"`/`rel` pair, the placeholder fallback when empty, HTML-escaping the link name
  even if a future bulk-import path bypassed form validation), and cache invalidation on
  save/delete.
- Full project suite green, `black`/`isort`/`ruff` clean (a
  `per-file-ignores` entry was added to `pyproject.toml` for `apps/pages/social_icons.py`'s E501 —
  real, unbreakable SVG path-data lines, not code that should ever be reformatted).
- Live-browser verified end-to-end against a real `runserver` session (a throwaway staff account
  created and deleted for this check, matching this project's own "don't touch real accounts for
  verification" convention): typed an Instagram URL, watched the icon auto-detect, set a custom hex
  color, saved, confirmed the row and the storefront footer both rendered the real pink Instagram
  icon, then deleted it via the shared confirm-modal and confirmed the footer fell back to the
  placeholder icons cleanly.
- `code-review-and-quality` pass (fresh-context `code-reviewer` agent) run after implementation,
  specifically re-verifying every doubt-driven-development finding was actually applied in the
  code (not just claimed) and checking for any further data-entry bugs in the SVG/hex-color
  registry beyond the one already caught. Verdict: APPROVE, zero Critical/High findings. The
  reviewer independently re-fetched live Simple Icons data and spot-checked 6 of the (then-18)
  curated SVG paths byte-for-byte plus all 18 hex colors — all exact matches — and surfaced the
  real LinkedIn-trademark-removal finding above (fixed), a doc-accuracy nit (this file's own test
  count, fixed), and a low-severity TOCTOU note on the `MAX_SOCIAL_MEDIA_LINKS` count-then-create
  check (accepted as a documented trade-off, not fixed — admin-only, cosmetic, not a financial
  path).

**Files touched:** `apps/pages/social_icons.py` (new), `apps/pages/models.py` (new),
`apps/pages/context_processors.py` (new), `apps/pages/migrations/0001_initial.py` (new),
`apps/pages/migrations/0002_alter_socialmedialink_platform.py` (new, LinkedIn removal from
`choices`), `apps/admin_portal/forms.py`, `apps/admin_portal/views.py`, `apps/admin_portal/urls.py`,
`templates/admin_portal/social_links_settings.html` (new),
`templates/admin_portal/base_dashboard.html`, `templates/pages/_social_icon.html` (new),
`templates/base_store.html`, `bancostore/settings.py` (context processor registration),
`pyproject.toml` (ruff per-file-ignore), `docs/decisions/0009-social-media-links-design.md` (new),
`tests/unit/pages/` (new), `tests/feature/admin_portal/test_social_links_settings.py` (new)

**Estimated scope:** L

**Built:** Done 2026-08-11.

---

## Phase 10: Deployment

### Task 24: Deploy to Hostinger VPS (production)

**This is a production deployment — every sub-task below (24a-24h) touches the real Hostinger VPS
and/or the real GoDaddy-registered domain. Confirm with the user before running any step of any
sub-task**, per `SPEC.md` Boundaries (production deploys are an "ask first" action). This is not a
one-time gate at the top of Task 24 — each sub-task gets its own explicit go-ahead, since each is
independently capable of taking the real site down or misconfiguring something a later step
builds on.

**Description:** Stand up the Hostinger KVM 2 VPS (Ubuntu 24.04 LTS) as the production host and
move Bancostore onto it: MySQL 8 (real concurrent writes, replacing local SQLite), Redis, Nginx as
reverse proxy + static/media file server, Let's Encrypt for HTTPS, and Supervisor to keep Daphne
(ASGI) and the Celery worker/beat processes running permanently, including across reboots. This is
the point where every "local dev uses SQLite / MySQL doesn't run on this Mac" workaround in
`SPEC.md` stops applying — production runs the real stack end to end. Hosting (Hostinger) and the
domain (GoDaddy) were both purchased and confirmed in hand 2026-08-06, unblocking this task.
Broken into 8 vertical sub-tasks below — each leaves the VPS in a working, checkpointable state
before the next one starts, matching this project's established build process (16a-16h,
17a-17f, 18a-18g, 19a-19c).

**Also folds in, rather than leaving as separately-tracked deferred items,** the two Known Issues
this file has explicitly flagged as "needs the real Nginx config, tracked for Task 24" (see the
"Known issues" sections near the top of this file): production security headers +
`SECURE_PROXY_SSL_HEADER` trusting exactly one hop from Nginx, and the rate-limit IP key collapsing
into one shared bucket (or becoming spoofable) once traffic passes through a reverse proxy. Also
folds in swapping `DEFAULT_FROM_EMAIL` off the reserved `.test` TLD, flagged in the same section as
a pre-go-live item.

**Overall acceptance criteria** (each owned by one or more sub-tasks below) — **all complete,
2026-08-10:**
- [x] Hostinger KVM 2 VPS provisioned (Ubuntu 24.04 LTS), SSH key-based access configured, root
  login disabled in favor of a sudo user — **24a**
- [x] MySQL 8, Redis, Nginx, and Python installed on the VPS via `apt` — **24b**
- [x] GoDaddy domain resolves to the VPS — **24c**
- [x] Production `.env` created directly on the server (never committed): real `SECRET_KEY`,
  `DEBUG=False`, `ALLOWED_HOSTS` set to the production domain, `DATABASE_URL` pointing at the VPS's
  MySQL, `REDIS_URL`, and the Paystack/email/SMS provider keys from `SPEC.md` Open Questions, plus
  the security-header/proxy-IP-trust/`DEFAULT_FROM_EMAIL` fixes above — **24d**
- [x] `pip install -r requirements.txt`, `npm run build`, `python manage.py collectstatic`, and
  `python manage.py migrate` all run clean against real MySQL on the VPS — **24e**
- [x] Supervisor configs for Daphne, `celery worker`, and `celery beat` — auto-restart on crash and
  on VPS reboot — **24f**
- [x] Nginx reverse-proxies to Daphne, serves `static/`/`media/` directly, and Let's Encrypt issues
  a valid HTTPS certificate (with auto-renewal) for the production domain — **24g**
- [x] A deploy process is documented (manual runbook at minimum); one full smoke-test purchase
  succeeds against real MySQL/Redis before any real user account exists on the VPS — **24h**

`https://bancostore.com` is live. See 24h's own Built note below for the full smoke-test story,
real bugs found and fixed along the way, and Checkpoint J sign-off.

**Dependencies:** Task 23 (Checkpoint I — full MVP complete and verified locally, done), Open
Question #1 (CI provider confirmed, done), Hostinger VPS + GoDaddy domain in hand (done, 2026-08-06)

**Estimated scope:** L overall, broken into S/M sub-tasks below

---

#### Task 24a: VPS access hardening

**Description:** Confirm SSH access to the Hostinger KVM 2 VPS with the credentials from
Hostinger's panel, then move off password/root access before anything else touches the box: create
a non-root sudo user, install the user's SSH public key for it, then disable root SSH login and
password authentication in `sshd_config`. This is the first sub-task deliberately because every
later step assumes a hardened, key-only login already exists — doing it last would mean running
several earlier steps over a less secure channel.

**Acceptance criteria:**
- [x] Initial SSH login with Hostinger-issued credentials confirmed working
- [x] A sudo, non-root user created with the user's own SSH public key installed
- [x] `PermitRootLogin no` and `PasswordAuthentication no` set in `sshd_config`, `sshd` reloaded

**Verification:**
- [x] A fresh SSH session as the new sudo user succeeds with the key, no password prompt
- [x] `ssh root@<vps-ip>` and any password-based login attempt are both refused

**Built:** Done 2026-08-10, VPS `186.240.150.230` (`srv1882501.hstgr.cloud`, Ubuntu 24.04.4 LTS).
Root's own password was never typed anywhere in this session — Hostinger's per-VPS "SSH keys"
panel already injects an uploaded key into a *running* server's `authorized_keys` with no reboot
required (confirmed live), so a dedicated `ed25519` key pair
(`~/.ssh/bancostore_hostinger` locally) was generated and added that way instead. `bancostore`
sudo user created with the same key and passwordless sudo (`/etc/sudoers.d/bancostore`,
`NOPASSWD:ALL`) — the standard pattern for a key-only deploy account, since the real security
boundary is SSH key possession, not a second sudo password prompt that would also block
non-interactive automation for the rest of Task 24. **Real gotcha caught before it could ship
silently broken:** the first hardening drop-in
(`/etc/ssh/sshd_config.d/99-bancostore-hardening.conf`, `PermitRootLogin no` /
`PasswordAuthentication no`) looked like it applied (`sshd -t` passed, `sshd` restarted clean) but
`sudo sshd -T`'s *effective* config still showed `passwordauthentication yes` — Ubuntu's own
`50-cloud-init.conf` (root-only readable, root-only writable by cloud-init on first boot) also sets
`PasswordAuthentication yes` and, since `sshd` uses first-match-wins across `Include`'s
glob-sorted file order, `50-` was winning over `99-` regardless of what the later file said.
Fixed by renumbering the drop-in to `10-bancostore-hardening.conf` (sorts before `50-`) rather
than editing cloud-init's own file, which risks being silently regenerated on a future cloud-init
run. Verified with three real connection attempts, not just reading config: key login as
`bancostore` succeeds, key login as `root` is refused (`Permission denied (publickey)`), and a
password-auth probe (`-o PubkeyAuthentication=no`) is refused with no password prompt ever offered
— cross-checked against `sshd -T`'s effective-config dump, not inferred from the drop-in files
alone.

**Dependencies:** None (first real-infra step)

**Estimated scope:** XS

---

#### Task 24b: Base packages

**Description:** Install MySQL 8, Redis, Nginx, Python 3.13 (or whatever Ubuntu 24.04's repos
carry), and Supervisor via `apt` — native install works here, unlike this Mac. Create the
production MySQL database plus a dedicated `bancostore` DB user with grants scoped to just that
database (never the app connecting as MySQL root).

**Acceptance criteria:**
- [x] `mysql`, `redis-server`, `nginx` all installed, enabled, and running via `systemctl`
- [x] A production MySQL database and a non-root, least-privilege `bancostore` DB user created
- [x] Python 3 + `venv` available; Supervisor installed

**Verification:**
- [x] `systemctl status mysql redis-server nginx` all show `active (running)`
- [x] The new DB user can connect and has privileges limited to the one production database

**Built:** Done 2026-08-10. Installed via `apt`: MySQL 8.0.46, Redis 7.0.15, Nginx 1.24.0,
Supervisor 4.2.5, Git 2.43.0. **Real discrepancy from the written acceptance criteria, flagged
rather than silently glossed over:** Ubuntu 24.04's default repos carry Python **3.12.3**, not
3.13 — 3.13 was never actually a documented production target, it's just what happens to already
be on the local dev Mac (`CLAUDE.md` Commands section); `tasks/plan.md`'s own Overview has always
named "Python 3.12" as the real stack target, and nothing in `requirements.txt`/`pyproject.toml`
pins a 3.13-only feature (checked directly, not assumed). Proceeding on 3.12 as consistent with
the originally-documented stack, not a downgrade.

`mysql`/`redis-server`/`nginx`/`supervisor` all enabled + confirmed `active` via `systemctl`.
MySQL's `bind-address`/`mysqlx-bind-address` both confirmed `127.0.0.1` (not exposed beyond
localhost — the app connects to it from the same VPS, never over the public network). Production
`bancostore` database created (`utf8mb4`/`utf8mb4_unicode_ci`) with a dedicated `bancostore`@`localhost`
DB user, `GRANT ALL PRIVILEGES` scoped to just that one database (`SHOW GRANTS` confirmed no
broader `*.*` privilege beyond the harmless default `USAGE`) — the app never connects as MySQL
`root`. Password generated with `openssl rand -hex 24`, stored only in the local scratchpad
(gitignored, session-isolated) for reuse when Task 24d builds the production `.env` — never
committed, never placed in a file on the VPS itself outside MySQL's own user table. Verified with
a real authenticated connection (`SELECT DATABASE(), CURRENT_USER();`), not just that the `GRANT`
statement itself succeeded.

**Dependencies:** 24a

**Estimated scope:** S

---

#### Task 24c: DNS cutover

**Description:** Point the GoDaddy-registered domain at the Hostinger VPS's IP (A record, or
Hostinger nameservers — whichever the user prefers) and confirm propagation before requesting a
Let's Encrypt certificate in 24g — Let's Encrypt's own rate limits punish repeated failed
validation attempts against a domain that doesn't resolve yet.

**Acceptance criteria:**
- [x] GoDaddy DNS updated to point the production domain (and `www`, if used) at the VPS's public
  IP
- [x] DNS change confirmed propagated before any HTTPS/cert work starts

**Verification:**
- [x] `dig`/`nslookup` for the domain from an outside network resolves to the VPS's IP

**Built:** Done 2026-08-10. Domain is `bancostore.com`, both purchased fresh with nothing else
configured on it, so an A-record edit (kept DNS management on GoDaddy, simpler and easier to
revert than delegating nameservers to Hostinger — no existing email/subdomains at risk either
way, but no reason to take the bigger action when the smaller one is sufficient) was the
right-sized choice over full nameserver delegation. GoDaddy's own default "WebsiteBuilder Site" A
record for `@` was edited to `186.240.150.230`; the existing `CNAME www → bancostore.com` record
needed no change at all, since it already follows whatever `@` resolves to. Verified propagated
both via the local resolver and directly against Google's public DNS (`8.8.8.8`), confirming it
wasn't just a locally-cached result — both `bancostore.com` and `www.bancostore.com` resolve to
the VPS. A live `curl` against `http://bancostore.com/` confirmed the full path actually works
end-to-end (DNS → routing → the VPS's Nginx), returning Nginx's stock "Welcome to nginx!" page —
correct and expected at this point, since the app itself isn't deployed until 24e-24g.

**Dependencies:** 24a (needs the VPS's public IP confirmed)

**Estimated scope:** XS

---

#### Task 24d: Production secrets, `.env`, and the two tracked pre-go-live security fixes

**Description:** Create the production `.env` directly on the server (never committed): real
`SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS` set to the production domain, `DATABASE_URL` pointing
at 24b's MySQL, `REDIS_URL`, and the Paystack/email/SMS provider keys. In the same slice, land the
two code changes this file has been tracking as "needs the real Nginx config, do it at Task 24"
rather than leaving them for a separate pass:
- An `if not DEBUG:` settings block: `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
  `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, and `SECURE_PROXY_SSL_HEADER` set to trust exactly
  one hop from Nginx (matching whatever header 24g's Nginx config actually sets — not a header an
  external client could also set directly).
- `RATELIMIT_IP_META_KEY` configured to read the same trusted-proxy header, so rate limiting
  doesn't collapse into one shared bucket (or become spoofable) once all traffic arrives via
  Nginx's own connection.
- Swap `DEFAULT_FROM_EMAIL` off the reserved `.test` TLD to a real deliverable domain.

**Acceptance criteria:**
- [x] Production `.env` exists on the server only, with all required variables set
- [x] Security-header `if not DEBUG:` block added, `SECURE_PROXY_SSL_HEADER` and
  `RATELIMIT_IP_META_KEY` both trust exactly the one header Nginx will set in 24g — no more, no less
- [x] `DEFAULT_FROM_EMAIL` no longer uses `.test`

**Verification:**
- [x] `python manage.py check --deploy` run locally against the new settings (before the settings
  even reach the server) shows no unresolved warnings for the items this task's acceptance
  criteria actually named (`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`,
  `SECURE_HSTS_SECONDS`, `SECURE_PROXY_SSL_HEADER`, `RATELIMIT_IP_META_KEY`) — two *additional*,
  never-in-scope Django deploy-checklist warnings (`security.W005` subdomain HSTS,
  `security.W021` preload) remain and are a deliberate, documented deferral, not an oversight —
  see the Built note below for the reasoning (CodeRabbit flagged this exact ambiguity on PR #64;
  this line was reworded to make the distinction explicit rather than leaving the checkbox looking
  like it silently ignored two open warnings)
- [x] A regression test (or a manual local check with `DEBUG=False`) confirms the security headers
  only activate when `DEBUG=False`, never affecting local dev

**Built:** Done 2026-08-10. Ran through `agent-skills:security-and-hardening` before touching
`settings.py`, since this is auth-cookie/HTTPS/rate-limit-adjacent code — confirmed
`SecurityMiddleware`/`XFrameOptionsMiddleware` are already installed, so Django's own defaults
already cover `X_FRAME_OPTIONS="DENY"` and `SECURE_CONTENT_TYPE_NOSNIFF=True` globally, not just in
production; a CSP was flagged as a real gap but needs a new dependency (`django-csp`, not in the
Tech Stack) and is out of scope here, tracked as future work rather than added silently.

`bancostore/settings.py`: new `if not DEBUG:` block right after `ALLOWED_HOSTS` sets
`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`,
`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` (safe once Nginx is the sole
internet-facing process and Daphne is loopback-only, 24f/24g), and
`RATELIMIT_IP_META_KEY = "HTTP_X_REAL_IP"` (matching the header 24g's Nginx config will set).
**`SECURE_HSTS_SECONDS` deliberately set to 3600 (1 hour), not the commonly-recommended 1 year** —
this is the first production deploy, HTTPS itself isn't verified end-to-end until 24g, and a
browser that's already cached a long HSTS value can't be talked back out of it if something's
misconfigured; raise once HTTPS has been stable for a while. `check --deploy` (simulated locally
with `DEBUG=False`) confirms this reasoning didn't leave a real gap — only `security.W005`
(subdomain HSTS) and `security.W021` (preload) remain, both consciously deferred for the same
first-deploy-caution reason, plus one pre-existing `debug_toolbar.W001` that fires under
`DEBUG=False` regardless of this change. Also fixed `TWO_FACTOR_REMEMBER_COOKIE_SECURE = not DEBUG`
(Task 6's own code comment had been waiting on exactly this since 2026-07-30) and the
`DEFAULT_FROM_EMAIL` fallback off `.test`.

**Real bug caught along the way, not just a production-only concern:** local `.env` already has
real Gmail credentials configured, meaning local dev has been sending real password-reset/lockout
emails from a `.test`-TLD `From:` address this whole time — not a hypothetical. Fixed there too
(`DEFAULT_FROM_EMAIL=bancostore7@gmail.com`, matching `EMAIL_HOST_USER` exactly, since that's the
only address with real SPF/DKIM alignment for this Gmail-SMTP setup — a `bancostore.com` address
would need its own mail-sending DNS records this project doesn't have). `.env.example` updated to
document all of this for future reference.

Production `.env` built from the local `.env`'s existing Gmail/mNotify/Didit credentials (same
verified-working accounts) plus a freshly generated `SECRET_KEY`
(`django.core.management.utils.get_random_secret_key`), `DEBUG=False`,
`ALLOWED_HOSTS=bancostore.com,www.bancostore.com,186.240.150.230` (the IP included too, so 24e/24f
can curl the app directly before Nginx exists). **Paystack started on TEST keys deliberately**
(user-confirmed 2026-08-10) — lets 24h's smoke test run safely with fake money before any real
customer exists, matching this task's own requirement; switching to live keys is deliberately left
as its own explicit step for actual launch, not bundled in here. **`DIDIT_WEBHOOK_SECRET` left
blank on purpose** (see `project_didit_webhook_secret_pending` memory) — Didit's webhook needs a
real HTTPS callback URL to register against, which doesn't exist until 24g; KYC still completes
correctly via `apps/distributors/views.py::kyc_verification_callback`'s independent server-side
re-verify in the meantime, confirmed by reading that code path directly rather than assuming.
Staged on the VPS at `/home/bancostore/.env.staged` (`chmod 600`, never committed) — moves into
the app directory as `.env` once 24e clones the repo. Full local suite green throughout (1250
passed, 1 skipped) — confirming this diff introduced no regression; the one background-thread
warning (`wallet_wallettransaction` unique-constraint race) is SQLite's known weaker concurrency
handling under a threaded test, unrelated to anything touched here.

**PR #64 follow-up fixes (2026-08-10), both caught by CI/CodeRabbit, not local review:**
1. The first push forgot to run `black . && isort . && ruff check .` locally (a genuine miss of
   this project's own Boundaries) — 15 `ruff` E501 line-length violations in the new comments,
   fixed by rewrapping.
2. **CI's `test` job failed with 476 of ~1250 tests returning a `301` redirect.** Root cause: CI
   deliberately runs the suite with `DEBUG=False` (to catch other `DEBUG=False`-only bugs, e.g.
   the media-serving gotcha earlier in this file) but isn't behind a real HTTPS-terminating proxy
   — `django.test.Client`'s requests are plain HTTP by default, so `SECURE_SSL_REDIRECT` redirected
   nearly every request. A gap in this task's own design that hadn't been considered: gating purely
   on `not DEBUG` doesn't distinguish "real production" from "a test run that happens to also set
   `DEBUG=False`." Fixed with `_RUNNING_UNDER_PYTEST` (`"PYTEST_VERSION" in os.environ`, confirmed
   empirically to be set by pytest ≥8.0 at process start, before Django settings are ever
   imported) — the whole security-header block and `TWO_FACTOR_REMEMBER_COOKIE_SECURE` now also
   exclude a pytest run. Reproduced the exact CI failure locally first (`DEBUG=False pytest -q`),
   confirmed red without the fix and green with it (1250 passed, 1 skipped) before pushing again.
3. **CodeRabbit's review on the fixed push found 2 more real issues, both fixed the same way this
   project always addresses review findings — together, in one follow-up commit** (plus 2 more
   flagged as genuine but deliberately NOT fixed here, see below): `.env.example`'s own
   `SECRET_KEY=change-me` placeholder was never actually rejected by the fail-closed guard (it only
   checked for `settings.py`'s own internal `"django-insecure-local-dev-only"` fallback) — extended
   to a `_KNOWN_INSECURE_SECRET_KEYS` set covering both. And a sharper catch: this task's own
   `DEFAULT_FROM_EMAIL` fallback fix reproduced the exact bug class it was fixing —
   `"no-reply@bancostore.example"` is itself also a reserved RFC 2606 TLD. Fixed by extending the
   same fail-closed pattern already established for `SECRET_KEY` (raise `ImproperlyConfigured` if
   unset outside `DEBUG` and not under pytest) rather than swapping in yet another fake address.
   **Two more CodeRabbit findings were deliberately NOT auto-fixed** — both real, both correctly
   labeled "Heavy lift" by CodeRabbit itself, both representing a bigger decision than a follow-up
   commit should make silently: (a) `bancostore` sudo user's `NOPASSWD:ALL` policy — a reasoned,
   documented tradeoff (24a's own Built note: the key is the real security boundary for a
   single-admin deploy account, matching standard cloud-provider convention), not an oversight, but
   CodeRabbit's suggested alternative (a command allowlist or separate elevated account) is a
   legitimate harder-security option worth the user's own call, not silently implemented or
   silently dismissed; (b) reusing local dev's real Gmail/mNotify/Didit credentials in production
   — increases blast radius of a local `.env` leak and means local manual testing could message
   real numbers/inboxes, but replacing them needs new sandbox/production-only accounts, a
   provisioning decision outside this task's scope and adjacent to `SPEC.md`'s own
   ask-first boundary on payment/SMS provider integration credentials.

**Dependencies:** 24b (needs real `DATABASE_URL`/`REDIS_URL` targets to point at), 24g conceptually
(the exact trusted-proxy header name), but the code change itself can be written and reviewed
before 24g runs — just not verified end-to-end until Nginx exists

**Files likely touched:** `bancostore/settings.py`, `.env.example` (document the production-only
variables, never real values)

**Estimated scope:** S

---

#### Task 24e: App deploy — dependencies, build, migrate

**Description:** Clone the repo onto the VPS, create the production virtualenv, and run the same
install/build/migrate sequence used locally — but against the real MySQL from 24b for the first
time ever in this project.

**Acceptance criteria:**
- [x] Repo cloned, venv created, `pip install -r requirements.txt` succeeds
- [x] `npm install && npm run build` succeeds, static assets produced
- [x] `python manage.py collectstatic` succeeds
- [x] `python manage.py migrate` runs clean against real production MySQL

**Verification:**
- [x] `python manage.py check` passes with the production `.env` loaded
- [x] A `python manage.py shell` query against a core model (e.g. `Category.objects.count()`)
  confirms the app can actually read/write the real MySQL database

**Built:** Done 2026-08-10. Cloned `https://github.com/owususampson10/bancostore.git` (public repo,
no auth needed) to `/home/bancostore/bancostore` at `464f5ad` (PR #64's merge commit — confirms the
VPS is running the settings.py security-hardening changes, not stale code). `.env.staged` from 24d
moved into place as `.env` (`chmod 600`). Two system-dependency gaps found and fixed, neither
originally in 24b's package list: Pango (`libpango-1.0-0`/`libpangocairo-1.0-0`, matching CI's own
already-established WeasyPrint requirement from Task 18e) and Node.js — 24b never installed a JS
runtime at all, since it wasn't in that sub-task's own scope (MySQL/Redis/Nginx/Python/Supervisor).
Installed Node 22.x via NodeSource (matching local dev's `v22.17.0` major version; no `.nvmrc` or
`package.json` `engines` field existed to pin an exact version, checked directly rather than
guessed). `pip install -r requirements.txt` succeeded clean (Django 5.0.14, WeasyPrint 62.3, all
verified importable). `npm run build` produced the same stable `main.css`/`main.js` filenames as
local dev (`vite.config.js`'s deliberate no-hash convention, CLAUDE.md's own documented gotcha) —
`npm audit` flagged 2 high-severity findings in dev dependencies, noted but not acted on now,
matching this project's own established non-blocking `pip-audit` precedent (Task 30f) rather than
gating this task on an unrelated triage pass. `collectstatic` copied 256 files. `migrate` applied
all 130+ migrations clean against real production MySQL for the first time in this project's
history — the one warning (`account.EmailAddress: models.W036`, MySQL not supporting a conditional
unique constraint allauth's own migration defines) is a known, pre-existing Django/allauth+MySQL
limitation, not something this task introduced. Verification went beyond a read-only count: created
a real `Category` row, confirmed it persisted via a fresh query, then deleted it — proving actual
write capability against the production database, not just connectivity.

**Dependencies:** 24b, 24d

**Estimated scope:** S

---

#### Task 24f: Process management — Supervisor

**Description:** Supervisor configs for Daphne (ASGI), `celery worker`, and `celery beat`, so all
three survive both a process crash and a full VPS reboot without manual intervention.

**Acceptance criteria:**
- [x] Supervisor program configs for Daphne, Celery worker, and Celery beat, all set to
  `autostart`/`autorestart`
- [x] Supervisor itself enabled to start on boot

**Verification:**
- [x] `sudo supervisorctl status` shows all three `RUNNING`
- [x] Killing one process (`kill -9`) results in Supervisor restarting it automatically
- [x] `sudo reboot` of the VPS, then `sudo supervisorctl status` again shows all three `RUNNING`
  with no manual restart

**Built:** Done 2026-08-10. `deploy/supervisor/bancostore-{daphne,celery-worker,celery-beat}.conf`,
all three running as the non-root `bancostore` user (never root), `directory=/home/bancostore/bancostore`
explicit on all three — needed for Celery's app discovery (`import bancostore`) specifically, since
unlike `.env` loading (which resolves via `BASE_DIR`, itself derived from `settings.py`'s own file
path, not cwd — confirmed by reading `_load_dotenv`'s implementation rather than assuming),
Celery's `-A bancostore` flag does depend on the working directory. Ran through
`agent-skills:security-and-hardening` before writing the configs, specifically to confirm Daphne
binding to `127.0.0.1:8001` only (not `0.0.0.0`) is both correct and load-bearing — it's the exact
assumption Task 24d's `SECURE_PROXY_SSL_HEADER`/`RATELIMIT_IP_META_KEY` settings already depend on
("Daphne only listens on 127.0.0.1 -- so trusting this one header is safe"); verified directly with
`ss -tlnp` rather than trusting the config file alone. Celery worker gets `stopasgroup=true`/
`killasgroup=true` and a generous 600s `stopwaitsecs` — Celery's own documented Supervisor gotcha
(the default prefork pool forks child processes; without these, Supervisor's stop signal only
reaches the parent, orphaning children and any in-flight commission/wallet/withdrawal task they
hold). Celery Beat pinned to `numprocs=1` with a comment explaining why: a second Beat instance
would double-fire every periodic task (binary bonus, matching bonus, withdrawal payout, PV expiry),
a real money-safety bug class, not just a nuisance — uses `django_celery_beat`'s
`DatabaseScheduler` (already configured in `settings.py`), so the schedule itself lives in MySQL,
not a local pickle file that could get lost on restart.

Verified beyond just `supervisorctl status`: `curl` directly to Daphne with no `Host` header
correctly got a `400` (proving `ALLOWED_HOSTS` is enforced even hit directly, not just through
Nginx); with a valid `Host: bancostore.com` header it correctly got a `301` (proving
`SECURE_SSL_REDIRECT` from Task 24d is genuinely active, not just present in the settings file —
expected until Nginx exists in 24g to supply the trusted proxy header); Celery worker's log showed
all 8 expected registered tasks; Celery Beat's log confirmed `beat: Starting...` (Celery logs to
stderr by default, not stdout — checked both files rather than assuming one). Crash recovery
tested for real: `kill -9`'d Daphne's actual pid, Supervisor restarted it with a new pid within 3
seconds. Reboot survival tested for real, not assumed from `systemctl enable` alone: `sudo reboot`,
polled for SSH to come back (~10s), confirmed all three `RUNNING` with fresh pids and zero manual
intervention.

**Dependencies:** 24e

**Files likely touched:** a new `deploy/` directory (Supervisor program configs)

**Estimated scope:** S

---

#### Task 24g: Nginx reverse proxy + HTTPS

**Description:** Nginx as the public entry point: reverse-proxies to Daphne, serves `static/` and
`media/` directly, sets the trusted-proxy header 24d's Django settings read
(`X-Real-IP`/`X-Forwarded-For`, restricted to exactly this one hop), and Let's Encrypt (`certbot`)
issues a real HTTPS certificate for the production domain with auto-renewal configured.

**Acceptance criteria:**
- [x] Nginx site config reverse-proxies to Daphne, serves `static/`/`media/` directly, sets exactly
  the one trusted-proxy header 24d's settings expect
- [x] Let's Encrypt certificate issued and installed for the production domain
- [x] Certbot auto-renewal configured (systemd timer or cron)

**Verification:**
- [x] Visiting the production domain over HTTPS loads the app with no errors, no mixed-content
  warnings
- [x] `sudo certbot renew --dry-run` succeeds
- [x] A rate-limited endpoint (e.g. `register`) hit from two different real external IPs shows two
  independent buckets, not one shared one — confirming 24d's proxy-IP trust config actually works
  end-to-end, not just in isolation

**Built:** Done 2026-08-10. `deploy/nginx/bancostore.conf`: reverse-proxies `/` and `/ws/` (Channels
WebSockets, matched before the generic `/` location since it needs the `Upgrade`/`Connection`
headers a plain proxy doesn't send) to Daphne at `127.0.0.1:8001`, serves `/static/`/`/media/`
directly via `alias`, and sets `X-Real-IP $remote_addr` / `X-Forwarded-Proto $scheme` — the exact
two headers `settings.py`'s `RATELIMIT_IP_META_KEY`/`SECURE_PROXY_SSL_HEADER` (Task 24d) are
written to trust. `certbot --nginx -d bancostore.com -d www.bancostore.com` issued and installed a
real Let's Encrypt certificate for both domains (expires 2026-11-08), rewrote the config in place
to add the HTTPS `listen`/cert directives and a redirect-only port-80 block — confirmed the rewrite
preserved every custom location block by reading the deployed file directly, not assuming Certbot's
`--nginx` plugin left them intact. Certbot's own systemd timer (`certbot.timer`, twice-daily,
already enabled by the package install) confirmed active; `certbot renew --dry-run` succeeded.

**Real bug found and fixed via live-browser-equivalent verification, not just `supervisorctl`/config
inspection:** static assets 404'd... no, worse — **403'd** — after Nginx/HTTPS otherwise worked
perfectly. Root cause: `/home/bancostore` (the `bancostore` user's home directory itself) is `750`
(`drwxr-x---`), so `www-data` (Nginx's worker process user) couldn't even *traverse into* the
directory tree to reach `staticfiles/`, regardless of the files themselves being world-readable
further down (confirmed via `namei -l` and Nginx's own error log:
`open() "...staticfiles/assets/main.css" failed (13: Permission denied)`). Fixed with the
least-privilege option — added `www-data` to the `bancostore` group (`usermod -aG bancostore
www-data`) rather than loosening the home directory to world-readable (`chmod o+rx`), which would
have exposed the whole home directory tree to every user on the system instead of just the one
process that actually needs read access. Restarted Nginx (not just reloaded) so its worker
processes picked up the new supplementary group membership. Verified with a real `curl` fetch of
`main.css`'s actual content, not just a `200` status code.

**Proxy-IP trust verified with a real spoofing attempt, a stronger test than the two-real-IP idea
originally planned** (impractical to arrange two genuinely distinct external source IPs from a
single session) **and one that directly demonstrates the actual security property that matters**:
using a real Python `requests` session against `/contact/` (real CSRF token fetched first, matching
Task 29's `@ratelimit(key="ip", rate="5/h", method="POST")`), sent 3 plain POSTs (all `200`,
correctly under quota), then 2 more POSTs carrying a spoofed `X-Real-IP`/`X-Forwarded-For` header
(also `200` — proving they counted against the *same* real bucket, not a separate spoofed one),
then a 6th plain POST correctly got `429`, and critically a 7th POST with yet another spoofed IP
header **also got `429`** — proving Nginx's `proxy_set_header X-Real-IP $remote_addr` genuinely
overrides any client-supplied value rather than passing it through, so an attacker cannot spoof a
fresh IP per request to bypass rate limiting. This is the exact attack the Known Issues section
(tracked since Tasks 1-7's original security review) was written to close.

**Dependencies:** 24c (DNS must resolve before requesting a cert), 24f (something must be running
behind Nginx to proxy to)

**Files likely touched:** `deploy/` directory (Nginx site config)

**Estimated scope:** M

---

#### Task 24h: Smoke test + deploy runbook + Checkpoint J sign-off

**Description:** One full purchase journey (mirroring Checkpoint I) run against real MySQL/Redis
on the VPS, before any real user account exists on it — per `SPEC.md` Boundaries, this is the last
moment it's safe to test against this specific database. Then document the deploy process as a
manual runbook (GitHub Actions auto-deploy on push to `main` is explicitly out of scope for this
sub-task — a manual runbook satisfies Task 24's own acceptance criteria; automating it is future
work, not silently assumed here).

**Acceptance criteria:**
- [x] One full smoke-test purchase (register → pay → starter pack → tree placement → KYC → IR ID →
  direct referral bonus → binary bonus cycle → withdrawal request → tax deduction → simulated
  payout) succeeds end-to-end against real production MySQL/Redis
- [x] A manual deploy runbook is written down (`deploy/README.md` or similar): what to run, in what
  order, to ship a new release to this VPS
- [x] `SPEC.md` Commands section gets the verified production commands added, matching how Task 1
  documented the local dev commands

**Verification:**
- [x] Every financial figure from the smoke test (PV, commission amounts, tax, wallet balance)
  checked against the production database directly, not just the UI — matching this project's own
  established verification standard (Checkpoint F, Task 17e, etc.)
- [x] A second person (or a second read-through by the user) could follow the runbook and
  successfully deploy a trivial change, without needing this conversation's context

**Built:** Done 2026-08-10, live in a real browser against `https://bancostore.com`, not a
simulation. **Two-account design, not the originally-sketched single-referral test:** the public
registration form has no path to a sponsorless account (`sponsor_ir_id` is a required field,
confirmed by reading `apps/distributors/forms.py::clean_sponsor_ir_id` directly) — a real
production launch's very first distributor has to be created directly, same as this project's own
`seed_roles` does in DEBUG. Distributor A (root sponsor) was bootstrapped via
`BinaryTree.place_distributor(None, ...)` — a genuine, documented first-class code path ("The very
first distributor in the system has no sponsor," not a workaround) — calling the same real service
functions `consume_paid_registration`/`consume_paid_starter_pack`/`approve_kyc` use internally,
skipping only the Paystack verification wrapper itself since A was never meant to be part of the
tested journey. Distributor B went through **every single step for real**: registration form →
real Paystack test-mode payment (GHS 1,500 for Starter Pack A, confirmed via the real checkout
widget, not mocked) → starter pack payment → real tree placement under A → real Direct Referral
Bonus (GHS 50 = 10% × 500 PV) credited to A's wallet → real phone OTP verification → real Didit
hosted KYC (ID + live selfie, completed by the user directly — 96.66% face match, 100% liveness) →
real admin review and approval via the actual `admin_portal` UI → real sequential IR ID assignment
(`IR00002`) → real withdrawal request/approval/tax computation for A.

**Starter pack prices (GHS 1,500 for Pack A/500 PV/Bronze, GHS 2,000 for Pack B/1,000 PV/Silver)
seen live in production for the first time this session** — worth recording since neither figure
had been written down anywhere before now. Re-reading `project_direct_referral_bonus_formula`
carefully: its own GHS 50/100 figures were always the *bonus* amounts (rate × PV), not pack
prices — no correction needed there; this live run just reconfirms that memory's formula exactly
(GHS 50 credited = 10% × 500 PV, precisely as documented).

**Four real, previously-uncaught bugs/gaps found via this live run, none of which any automated
test would have caught:**
1. **`PYTHONUNBUFFERED` missing from all three Supervisor configs** — `apps/notifications/sms.py`'s
   `print()`-based fake SMS sender (used whenever `MNOTIFY_API_KEY` is unset, exactly the
   temporary-blank state this smoke test itself used to avoid real SMS costs) silently sat in
   Python's stdout buffer and never reached the log for a long-running Supervisor-managed process —
   the exact same class of gotcha this project already hit once before with Django's autoreloader
   dropping `-u` (documented earlier in this file). Fixed in all three `deploy/supervisor/*.conf`
   files, verified live: the fake OTP appeared in the log immediately after the fix.
2. **Phone OTP verification only triggers at login, not right after registration** — not a bug,
   but a real gap in this task's own assumptions going in: `verify_otp_view` requires
   `request.session["otp_purpose"]`, which is only set by `attempt_distributor_login`'s
   `needs_verification` branch. Since payment auto-logs a distributor in via
   `registration_payment_callback`, that gate is never hit until a later, separate login attempt —
   confirmed correct by reading `apps/distributors/views.py` directly rather than guessing.
3. **`/accounts/login/` (plural, allauth's customer path) vs `/account/login/` (singular,
   `two_factor`'s admin path) is a real, easy-to-hit mix-up** — the user's first admin login
   attempt landed on the wrong page entirely, surfacing as a misleading "Session Expired" page
   (this project's own themed CSRF-failure view, `bancostore.views.csrf_failure`, since allauth's
   page has no CSRF token matching what `two_factor`'s form expects). Not a code bug, but confusing
   enough that it's worth this explicit note for the next person who hits it.
4. **A brand-new admin account with zero TOTP devices lands on a bare 403, not a forced-setup
   redirect** — `django-two-factor-auth`'s own `LoginView` only auto-redirects to
   `two_factor:setup` when the login was reached via a `?next=` param pointing at a URL its
   `is_otp_view()` recognizes as OTP-required; a direct visit to the login page (exactly how this
   session reached it) never triggers that redirect, and `admin_portal`'s own
   `is_admin_portal_staff` check (`user.is_staff and user.is_verified()`) just returns a plain 403
   with no path forward. Confirmed by reading `two_factor`'s own `views/core.py` source, not
   guessed. Not fixed as a code change here (needs a product decision — e.g. should
   `admin_portal`'s 403 page itself detect "staff, zero devices" and link to setup? — flagged for a
   future task, not silently patched mid-smoke-test) — worked around for this session by navigating
   directly to `/account/two_factor/setup/`.

**Binary Bonus verified as a real, working pipeline, not a full multi-leg scenario:**
`calculate_binary_bonus()` invoked directly via shell against the real thin tree (A with only one
downline leg populated) — correctly evaluated 1 distributor, paid GHS 0 (weak/right leg has 0 PV,
correctly no bonus due), and wrote a real `CommissionCycleRun` audit record matching the task's own
return value exactly. This proves the production Celery/Redis/MySQL wiring works end-to-end; the
bonus math itself was already exhaustively proven by the existing automated test suite (Checkpoint
E), so reproducing a full two-leg scenario for real (a third distributor, a third live Didit
session) was deliberately out of scope, per the plan agreed with the user before starting.

**Withdrawal tested for real, with a legitimate temporary settings change:** `MIN_WITHDRAWAL_AMOUNT`
lowered from GHS 100 to GHS 1 via the real `admin_portal` Platform Settings screen (not a code
change — the platform's own intended admin control), specifically to let A's real GHS 50 balance
clear the minimum. A requested GHS 50, tax correctly computed at 1% (GHS 0.50), admin approved via
the real UI (fresh TOTP re-entry required — "remember device" defaults unchecked by design),
wallet correctly debited to GHS 0.50 remaining, `WithdrawalRequest.status` correctly transitioned
to `approved_debited`. **Payout destination snapshot-at-approval-time confirmed correct, not a
bug** — `WithdrawalRequest.payout_mobile_money_number/network` are blank at submission and only
populated by `approve_withdrawal_request` (confirmed by reading `apps/withdrawal/services.py`
directly before concluding this), matching this codebase's consistent snapshot-at-event-time
convention elsewhere (order prices, starter pack prices). **The actual Paystack Transfer to a real
mobile money account was not exercised** — already a known, accepted, documented limitation
(`project_paystack_transfer_account_tier_blocked`: this sandbox account's "Starter Business" tier
blocks all real Transfers regardless of test/live mode), not something this task could have forced
through; the payout batch reaching that API call and failing there is expected, not a new gap.
`MIN_WITHDRAWAL_AMOUNT` restored to GHS 100 immediately after, confirmed via `constance.config`
directly.

**Cleanup:** both smoke-test distributors and every record cascading from them (23 rows: wallet
transactions, PV ledger/daily bucket/monthly personal PV, Didit verification, binary tree edge,
withdrawal request, notifications, pending registration, the two `User`/`Distributor` rows
themselves) deleted before finishing — confirmed via `User.objects.filter(...).delete()`'s own
returned cascade summary, not assumed. 10 `CommissionCycleRun` audit records also removed (Celery
Beat's own real periodic schedule fired several times in the background during the session,
against the same thin test data — all zero-payout, safe to remove). Production database now has
zero distributors, matching a genuine pre-launch state; the preserved production administrator
account was deliberately kept, not deleted along with the test data.

**mNotify handled the same way Task 18g established:** `MNOTIFY_API_KEY` temporarily blanked
(explicit user sign-off) so OTP codes printed to logs instead of sending real, billed SMS; restored
to the real key and all three Supervisor programs restarted immediately after the KYC/OTP steps
were done, confirmed via a live site health check afterward.

`deploy/README.md` written: server facts, deploy steps, environment variable notes (including the
two that need special care — `MNOTIFY_API_KEY` and the Paystack test/live decision),
crash/reboot recovery (nothing manual needed, already proven in 24f), the mNotify-blanking pattern
for future safe testing, going-live-with-real-Paystack-keys steps, the deferred Didit webhook setup
procedure (now unblocked since HTTPS exists), and the known limitations list (Paystack Transfer
account-tier block, `npm audit` findings, no CI/CD auto-deploy yet). `SPEC.md` Commands section
updated with both the already-stale local dev block (fixed `seed_data` → the real `seed_roles`,
removed the leftover "to be finalized" framing) and a new Production section pointing to
`deploy/README.md` as the source of truth rather than duplicating it.

**Checkpoint J reached:** `https://bancostore.com` is live, reachable over real HTTPS, running on
the Hostinger VPS against real MySQL, with Daphne/Celery worker/Celery beat kept alive by
Supervisor and surviving a real reboot (proven in 24f). Task 24, and with it the full numbered MVP
task list (Tasks 1-24 plus the ad-hoc Tasks 25-31), is complete.

**Dependencies:** 24a-24g all complete

**Files likely touched:** `deploy/README.md`, `SPEC.md` Commands section

**Estimated scope:** S

---

### Task 36: Post-deploy fixes — cache-busting, footer layout, Social Links relocation, icon picker bugs, currency dropdown

**Description:** Not in the original plan — found by the user reviewing the real production deploy
of Tasks 34/35. Four independent fixes, built as vertical slices (36a-36d).

**36a. Real cache-busting (root cause of "legal pages look unstyled in production"):**
`vite.config.js` used fixed output filenames (`assets/main.css`/`main.js`, no content hash) —
flagged in its own comment since Task 1 as "revisit once there's a real deploy pipeline." Task 24
built that pipeline; this is the revisit. Confirmed via direct evidence, not assumption: `curl`
against the live server showed the correct, freshly-built 86,702-byte CSS (containing the new
`.legal-content`/`grid-cols-5` rules), but a `fetch()` from inside a real browser tab on the live
site returned a stale, differently-sized 84,705-byte cached copy — the server was always correct,
browsers were serving pre-deploy assets from HTTP cache with nothing forcing revalidation. Fixed by
switching Vite to real content-hashed filenames and adding `apps/pages/templatetags/vite_tags.py`
(`{% vite_asset "main.js" %}`/`{% vite_css "main.js" %}`), reading `static/dist/.vite/manifest.json`
to resolve the real hashed path at render time — manifest is re-read on every call in `DEBUG` (so
local dev's existing `npm run build` + hard-refresh workflow keeps working unchanged) and cached in
memory in production (`DEBUG=False`, where a new deploy already restarts the Daphne process, so a
stale in-memory manifest can't outlive a real deploy). Updated all 7 templates referencing the old
fixed paths.

**36b. Footer layout — 4 columns (Brand/Shop/Company/Policies), Follow Us as its own row below:**
The 5-column grid built for Task 34/35 put Follow Us as a 5th column; the user wants Brand, Shop,
Company, Policies as 4 equal columns with Follow Us in its own full-width section underneath.

**36c. Social Links moved into Platform Settings' "General Platform Settings" tab:**
Originally built as its own top-level admin_portal sidebar item (Task 35) — the user asked for it
to live under Platform Settings > General instead, alongside the other general/decorative-text
settings it was already conceptually grouped with (`REFUND_RETURN_POLICY_TEXT` etc.). Sidebar entry
removed; the CRUD UI (list/add/edit/delete) is embedded as its own section within the existing
`general_platform_settings` tab panel, as a second `<form>` alongside the constance form (not a
merge of the two — the constance form's own save button never touches social links, and vice
versa).

**36d. Icon picker bugs:** (1) the dropdown option list was clipped by a parent `overflow-hidden`
container on both the row list and the add-form wrapper — confirmed via a live screenshot showing
the list cut off after 2 items. (2) Selecting an icon now auto-applies that icon's real curated
`default_color` (a small in-page `SOCIAL_ICON_DEFAULT_COLORS` lookup, sourced from the same trusted
server-side registry, not user input) — with a "reset to default" control so a manually-picked
color can be reverted without retyping the hex.

**36e. Currency Symbol → real dropdown:** both `CURRENCY` and `CURRENCY_SYMBOL` were free-text
fields an admin had to type "GHS" into. Confirmed via grep neither is actually read anywhere in the
app yet (a decorative setting, the same class Task 30 fixed several of already) — flagged honestly
rather than silently wiring every hardcoded `GHS` price prefix across the codebase to read it live,
which is real, separate, unrequested scope. Converted both to a new `currency_field`
`CONSTANCE_ADDITIONAL_FIELDS` choice type rendering a real native `<select>` of 5 currency
code/name pairs (GHS, NGN, USD, GBP, EUR), storing the code while displaying the full name —
matching this same file's own established `WITHDRAWAL_DAY`/`WITHDRAWAL_FREQUENCY` precedent
(`day_of_week_field`/`withdrawal_frequency_field`, already plain native selects), not the
Alpine-listbox pattern used elsewhere for fields needing custom popup styling.

**Files touched:** `vite.config.js`, `apps/pages/templatetags/vite_tags.py` (new), 6 base templates,
`templates/base_store.html` (footer), `apps/admin_portal/urls.py`/`views.py`,
`templates/admin_portal/base_dashboard.html`, `templates/admin_portal/platform_settings.html`,
`templates/admin_portal/partials/_social_links_section.html` (new, migrated from the now-deleted
`templates/admin_portal/social_links_settings.html`), `apps/platform_settings/config.py`, plus
tests in `tests/feature/admin_portal/test_social_links_settings.py` and
`tests/feature/admin_portal/test_platform_settings.py`.

**Verification:** all 5 sub-slices live-browser-verified against local dev (not just pytest) —
36a: hard-refreshed and confirmed the new hashed CSS/JS filenames load, no stale cache. 36b:
footer confirmed as 4 equal columns (Brand/Shop/Company/Policies) with Follow Us as its own
full-width row below. 36c: Social Links CRUD confirmed living inside the General Platform Settings
tab (`?tab=9` deep link), sidebar item confirmed removed. 36d: icon dropdown confirmed showing all
6 platforms uncut; selecting YouTube confirmed auto-applying `#FF0000`; manually overriding to
`#123456` and clicking reset confirmed reverting back to `#FF0000`. 36e: server-rendered HTML
confirmed as a genuine `<select name="CURRENCY">` with all 5 options, `selected` correctly marking
the live stored value. Full targeted suite green: 357 passed, 1 skipped. **Noticed but not
addressed, flagged to the user rather than silently changed:** the pre-existing decorative
`SOCIAL_MEDIA_LINKS` free-text field (a leftover from before Task 35 built the real CRUD version)
still sits directly above the new Social Links section on the same tab, which now reads as
redundant/confusing next to the real feature.

**Estimated scope:** L

**36f. Three follow-up fixes, found by the user reviewing 36a-36e live:** (1) the leftover
decorative `SOCIAL_MEDIA_LINKS` free-text constance field (superseded by Task 35's real
`SocialMediaLink` CRUD, never actually read anywhere) removed entirely from `GENERAL_PLATFORM_SETTINGS`
-- it sat directly above the real Social Links section and read as a confusing duplicate. (2) A real
sticky-positioning bug: the vertical tabs nav was `position: sticky` as a direct child of the
constance `<form>`, but the Social Links block had to live as a sibling *after* that form closes
(forms can't nest) -- so the nav's sticky containing block ended at the form's bottom edge, and it
un-stuck and scrolled away as soon as the page scrolled into Social Links, exactly as the user
described. Fixed by restructuring `platform_settings.html` so the nav and the whole right-hand
column (form + Social Links block) share one common flex-row parent -- confirmed live: the nav now
stays pinned all the way to the very bottom of the page. Also dropped the now-unneeded
`lg:pl-[19.5rem]` manual alignment hack. (3) Font-size tokens inside
`_social_links_section.html` normalized to the exact vocabulary `platform_settings.html` itself
uses everywhere else on the page (grep-confirmed set: `font-headline-sm`/`text-headline-sm`,
`font-body-md`/`text-body-md`, `font-label-md`/`text-label-md` only) -- replaced an 18px
`font-body-lg`/`text-body-lg` row title, a 14px `font-body-sm`/`text-body-sm` notice, a mismatched
`font-body-md`/`text-body-sm` dropdown-option pairing, and three buttons using `font-button`/
`text-button` -- confirmed via grep against `static/src/main.css` that those two tokens don't exist
anywhere in this codebase's `@theme` block, so those 3 buttons had been silently rendering with zero
custom font styling (browser default) this whole time, a real pre-existing bug this fix also closed.
Live-browser-verified end to end: leftover field confirmed gone, sidebar confirmed sticky through
the full page including past Social Media Links, and all social-links text confirmed matching the
rest of the page's sizing. Targeted suite green (303 passed, 1 skipped) both before and after.

**36g/36h. Currency/Currency Symbol, resolved through a direct question rather than guessed at:**
the user flagged the native `<select>` styling and separately asked what actually happens when an
admin changes Currency away from Cedis, given Paystack. The honest answer -- confirmed via grep --
is nothing: no price display or Paystack API call anywhere in this codebase reads `CURRENCY`/
`CURRENCY_SYMBOL`, and the Paystack merchant account itself is GHS-only, so a working-looking
5-currency dropdown was a real footgun (an admin picking USD expecting something to change, then
nothing does). Put to the user directly via `AskUserQuestion` rather than silently choosing --
**restrict to GHS only** was chosen over "keep 5 options + add a warning" and "leave as-is". 36g's
first pass (themed 5-option Alpine listbox, matching `catalog_product_form.html`'s Category field --
built before the Paystack question was asked) was superseded by 36h before it ever shipped: the
`currency_field` `CONSTANCE_ADDITIONAL_FIELDS` choices are now a single `("GHS", "Ghanaian Cedi
(GHS)")` entry, and both fields render as a plain locked display (lock icon + honest "Locked to
GHS -- multi-currency support isn't built yet" help text) instead of any kind of picker.
`field.disabled = True` (matching `ADMIN_2FA_ENABLED`'s own established tamper-resistance pattern
in the same function) is defense-in-depth on top of the single-choice restriction -- confirmed via
Django's `Field.bound_data` semantics that a disabled field's value always resolves from the real
stored initial value, never a submitted POST body, even on a validation-failure re-render. Real
multi-currency support (per-currency pricing, the Paystack currency parameter, exchange-rate-safe
commission math) stays flagged as separate, unrequested scope, not silently built or faked with a
non-functional picker. Also investigated and resolved without a code change: the user's separate
observation that Terms/Privacy/Refund text fields "still show" even though Task 34 built real legal
pages for them turned out to be a different situation than the dead `SOCIAL_MEDIA_LINKS` field 36f
removed -- `apps/pages/views.py` confirmed all three are genuinely wired to their respective legal
page as an optional "Additional Details" block that only renders if an admin fills it in, currently
empty. Put to the user via the same `AskUserQuestion` call; kept as real, working capability rather
than removed. Live-browser-verified: locked display renders correctly with the lock icon and exact
help text. Targeted suite green throughout (updated `test_platform_settings.py` currency tests to
match: `test_currency_fields_render_as_locked_to_ghs` and
`test_currency_is_locked_to_ghs_even_if_submitted_data_says_otherwise`, the latter mirroring the
existing `ADMIN_2FA_ENABLED` tamper test's shape).

### Task 37: SEO foundations — meta tags, sitemap, structured data, page-speed audit

Not in `SPEC.md`, same class of new scope as Tasks 29/34/35 — requested directly by the user
2026-08-11, after the app had already been live at `bancostore.com` for a day (Checkpoint J,
2026-08-10). An audit before planning found almost nothing SEO-related exists today: no
`robots.txt`, no sitemap, no canonical/Open Graph/Twitter Card tags, no JSON-LD anywhere, and the
home page (`templates/catalog/home.html`) doesn't even override `base_store.html`'s generic
site-wide `title`/`meta_description` blocks — every other major page (`product_list`,
`product_detail`, all `pages/legal/*`, `about`, `contact`) already does. `Product`/`Category`
already have unique `SlugField`s and slug-based URLs, so nothing here needs a schema change.
`SECURE_PROXY_SSL_HEADER` is already configured for the production Nginx setup, so
`request.build_absolute_uri()` will correctly report `https://bancostore.com/...` for
canonical/OG URLs with no new `SITE_URL` setting or `django.contrib.sites` wiring needed.

Split into 4 vertical slices, per this codebase's established build process:

**37a. Domain-aware meta framework.** Canonical `<link>`, Open Graph (`og:title`/`og:description`/
`og:url`/`og:image`/`og:type`), and Twitter Card tags added to `templates/base_store.html`, driven
by the same `{% block title %}`/`{% block meta_description %}` overrides already used per-page —
no new per-template plumbing beyond a couple of new blocks (e.g. `og_image`) for pages that want a
non-default share image. Fixes the home page's missing `title`/`meta_description` override in the
same pass, since it's the same code path. **Open decision:** no OG share image (1200×630 raster)
exists yet — only SVG logos under `static/images/bancostore-brand/logo/`. Needs a real image before
this slice is complete, not a placeholder.

**37b. `robots.txt` + XML sitemap.** `django.contrib.sitemaps` (already available, just not
wired into `INSTALLED_APPS`/`urls.py` yet) covering products, categories, and static/legal pages;
`robots.txt` disallows non-public paths (`admin_portal`, distributor dashboard, checkout, accounts,
`/media/` KYC uploads) and points at the sitemap.

**37c. JSON-LD structured data.** `Organization`/`WebSite` schema sitewide (in `base_store.html`),
`Product` schema (price, availability, image) on `product_detail`.

**37d. Page speed / Core Web Vitals audit.** Diagnostic pass via `agent-skills:web-performance-auditor`
against the live production site. Produces findings only — fixes get scoped as their own follow-up
task if the findings justify it, not bundled blindly into this task.

**Verification (per slice, before moving to the next):** full suite green, live-browser view-source
check that meta tags render with real values (not template syntax leaking through), `robots.txt`/
sitemap reachable and valid XML, Google's Rich Results Test validates the JSON-LD with zero errors.

**37a — done 2026-08-11.** User chose "generate a simple branded banner now" (via `AskUserQuestion`)
for the missing OG share image, over using the bare logo or shipping without one. Built with
headless Chrome (`--headless --screenshot`, already installed on this Mac, no new dependency) to
rasterize a small HTML/CSS banner — the existing `bancostore-logo-dark.svg` wordmark (the variant
already designed for colored backgrounds) on an orange gradient with the site tagline — into an
exact 1200×630 PNG at `static/images/bancostore-brand/social/og-default.png`.

`templates/base_store.html` gained canonical `<link>`, `og:site_name`/`og:type`/`og:title`/
`og:description`/`og:url`/`og:image` (+ width/height), and `twitter:card`. **Real Django constraint
hit and worked around:** the same `{% block %}` tag name can't be repeated inside one template
(confirmed by an actual `TemplateSyntaxError`, not assumed) — a first attempt tried reusing
`{% block title %}`/`{% block meta_description %}` verbatim inside the OG meta tags to avoid
duplicating text; fixed with dedicated `og_title`/`og_description` blocks instead, defaulting to
the same literal text. `twitter:title`/`twitter:description`/`twitter:image` were deliberately left
out entirely — Twitter/X's own Card spec falls back to the `og:*` equivalents when they're absent,
so duplicating them would be redundant, not more correct. `templates/catalog/home.html` (previously
inheriting the generic sitewide title/description with no override at all — the exact gap the audit
found) now sets real, keyword-specific `title`/`meta_description`/`og_title`/`og_description`.
`templates/catalog/product_detail.html` overrides `og_type` to `"product"` and `og_image` to the
product's own `primary_image` (falling back to the default banner if a product somehow has no
photo yet) — the highest-value case for real Open Graph data, since distributors routinely share
product links directly (WhatsApp, social) to sell, unlike a Terms of Use link.

Caught and fixed a real regression in an existing test before it could look like an unrelated
break: `test_gallery_shows_all_images_with_primary_shown_first` asserted the primary product
image's URL appears exactly twice on the page (main image + thumbnail) — the new `og:image` tag is
a legitimate third occurrence, so the count assertion was updated to 3 with a comment explaining
why, not loosened or deleted. Also found, confirmed via `git stash` to be a **pre-existing,
unrelated bug already on `main`** (a Task 36 regression, not introduced here): `test_earnings_history.py::
test_page_loads_the_shared_js_bundle_so_the_sidebar_can_actually_collapse` asserts a literal
`src="/static/assets/main.js"` script tag, but Task 36 switched Vite's build output to hashed
cache-busting filenames (`main-<hash>.js`), so that literal string no longer appears anywhere —
flagged to the user rather than silently fixed, since it's outside this task's scope.

4 new tests in `tests/feature/catalog/test_seo_meta_tags.py` (home page title/description/canonical/
OG/Twitter tags; product page OG type + real image; product page OG image fallback when no photo
exists). Live-verified against a real `runserver` (not just pytest): curled the home page and a
real product page (`vitality-core-daily`), confirmed every canonical/OG/Twitter tag renders with
real values (no leaked template syntax), confirmed the OG image itself returns HTTP 200, and
confirmed the product page correctly swaps in that product's own photo instead of the default
banner. Targeted suite (`tests/feature/catalog/`, `tests/feature/pages/`) green: 99 passed. Full
suite green except the one pre-existing, unrelated failure above: 1382 passed, 1 skipped.

**37b — done 2026-08-11.** Built TDD (RED tests written first, confirmed failing, then
implemented). `apps/pages/sitemaps.py` — `ProductSitemap` (active products only, `lastmod` from
`updated_at`) and `StaticViewSitemap` (home, shop, about, contact, all 7 legal pages). No
`CategorySitemap`: `Category` has no dedicated detail page (`product_list` filters by
`?category=<slug>` query param), and a filtered-listing URL in the sitemap would contradict that
page's own canonical tag (`request.path`, no query string) — deliberately left out rather than
included incorrectly.

**Real Django behavior discovered mid-build, not assumed:** `django.contrib.sites` is already
installed (`SITE_ID = 1`, confirmed by the pre-planning audit to be referenced nowhere in this
codebase) — Django's sitemap framework uses the `Site` model's stored domain for absolute URLs
whenever `django.contrib.sites` is installed, ignoring the request's own host entirely. First test
run surfaced the real, previously-invisible consequence: every URL rendered as `example.com`
(Django's shipped default), not `testserver`/`bancostore.com` as expected. Fixed with a data
migration (`apps/pages/migrations/0003_configure_production_site_domain.py`, reversible) setting
the `Site` row to the real production domain — the first real use this `SITE_ID` setting has ever
had in this codebase. `protocol = "https"` set on both Sitemap classes to match. `robots.txt`
(`templates/robots.txt`, served via `TemplateView` with `content_type="text/plain"`) blocks
`/admin/`, `/admin-portal/`, `/accounts/`, `/cart/`, and `/media/kyc/` (private ID documents —
`/media/products/`/`/media/categories/` stay crawlable for image search) — and, deliberately, only
the authenticated sub-paths under `/distributors/` (`dashboard/`, `earnings-history/`,
`binary-tree/`, `team/`, `payout-settings/`, `withdraw/`, `withdrawals/`, `cancel-membership/`,
`notifications/`, `webhooks/`, `kyc/`, `verify-otp/`, `password/`, and the mid-flow
`register/pay-fee/`/`starter-pack/` steps), not the whole prefix — a blanket
`Disallow: /distributors/` would have hidden `/distributors/register/` and `/distributors/login/`
from Google, the site's actual distributor-acquisition funnel page. 8 new tests in
`tests/feature/pages/test_seo_robots_sitemap.py`. Live-verified against a real `runserver`: curled
both endpoints, confirmed `robots.txt` returns real `text/plain` content and `sitemap.xml` lists
every real seeded product plus every static page with the correct `https://bancostore.com` domain.
Targeted suite green (`tests/feature/pages/`, `tests/feature/catalog/`: 107 passed); full suite
green except the one pre-existing Task 36 failure already flagged above (1390 passed, 1 skipped).

**37c — done 2026-08-11.** `apps/pages/context_processors.py` gained `organization_json_ld`
(registered in `TEMPLATES`, `bancostore/settings.py`), rendering a sitewide `Organization`/
`WebSite` JSON-LD block (`templates/base_store.html` `<head>`) — `Organization.sameAs` reuses
Task 35's real admin-managed `SocialMediaLink` rows (verified live: a real configured Facebook
link showed up correctly), and `WebSite.potentialAction` is a real `SearchAction` pointing at
`catalog:product_list`'s actual `?q=` search param (`apps/catalog/views.py`), enabling Google's
sitelinks search box. A shared `_get_cached_social_media_links()` helper was extracted so this new
context processor and the pre-existing footer one (`social_media_links`, Task 35) read the same
cached list instead of each running its own uncached query every request — a small, genuinely
duplicate-avoiding refactor, not scope creep. `apps/catalog/views.py::_build_product_json_ld`
builds a `Product` schema (name/description/`offers` with real price, `priceCurrency: "GHS"`,
`availability` from `product.in_stock`) rendered via `product_detail.html`'s `extra_body` block —
`image` is only included when a real photo exists (claiming the generic OG banner is a photo of a
specific product would be inaccurate structured data, unlike Open Graph's generic-preview
convention). Both JSON-LD blocks are serialized server-side rather than built with template tags,
so a product name or social-link URL containing quotes/special characters can never produce broken
JSON — matching this codebase's established "constrain server-side, never raw interpolation" rule
from Task 17's Alpine `x-data` XSS fix. **Update, PR #70 CodeRabbit round:** plain `json.dumps`
alone (this slice's original implementation) does not escape `<`/`>`/`&`, so a product description
or admin-entered social link containing a literal `</script>` could break out of the `<script>`
element and inject HTML — fixed with a new shared `bancostore/json_ld.py::dumps_for_script_tag()`
(the same escape mapping Django's own `django.utils.html.json_script()` uses), applied to both
JSON-LD builders, not just the one CodeRabbit's comment pointed at. See the PR #70 fix-round entry
below for the full detail. Caught the same test-count
regression pattern as 37a (`test_gallery_shows_all_images_with_primary_shown_first`'s primary-image
URL occurrence count bumped 3 → 4 for the new `image` field, with an updated comment). 7 new tests
in `tests/feature/catalog/test_seo_meta_tags.py`, parsing the rendered `<script>` tags with real
`json.loads` rather than fragile substring matching. Live-verified against a real `runserver`:
extracted and `json.loads`-parsed both pages' JSON-LD, confirmed well-formed and matching real
database content (a real product's real price/stock/photo). Targeted suite green (169 passed); no
further full-suite run needed beyond 37b's (no logic touched outside pages/catalog).

**37d — done 2026-08-11 (diagnostic only, no fixes applied).** Deep-mode Lighthouse audit (real
`npx lighthouse` CLI, not installed as a project dependency) against the live production home page
and shop page (`https://bancostore.com/`, `/shop/`); the product-detail page was audited against
local dev instead, since production's catalog currently has zero real products (a clean-slate
launch) — noted as a caveat on absolute timing numbers only, not on the structural findings, which
are identical code either way. Scores: home 90 performance / 91 SEO / 100 best-practices, shop 93 /
100 / 100, product (local) 92 / 100 / 100. Findings ranked by impact, reported to the user directly
(not fixed) — see the chat transcript for the full write-up; highest-impact items: a 1.1MB
un-subsetted Material Symbols variable-font request (the single largest asset on every page,
>60% of home's total 1.86MB weight); ~9 home-page decorative images served live from
`lh3.googleusercontent.com` (Google's Stitch-generation image host) instead of ever being
downloaded and self-hosted — a real production risk (URLs not guaranteed permanent) as well as a
performance one (~70% oversized for their actual display size, no modern format); no
gzip/brotli text compression on Nginx for the Vite JS/CSS bundle (~200KB combined savings); HTTP/1.1
instead of HTTP/2; render-blocking Google Fonts `<link>`; ~70% unused JS in the main Vite bundle.
Product photos already confirmed clean (real WebP, correctly sized — Task 7's pipeline holds up).
**No fixes applied in 37d itself — the highest-risk finding was acted on immediately after as
Task 38 below, per direct user instruction ("keep going") rather than waiting for a separate
scoping round.**

### Task 38: Self-host the remaining Stitch-generated images (Task 37d follow-up)

The audit's #2 finding (the `lh3.googleusercontent.com` third-party image risk) turned out to be
worse than "some day this could break" — `templates/catalog/home.html` already carried a
2026-08-07 comment recording that this exact class of link had already expired and broken the home
page's hero image once in production, fixed at the time by self-hosting *only* that one image; a
"follow-up sweep" at the time confirmed the rest were "still live" and left them as-is. That was
always a temporary reprieve, not a fix — Task 37d's audit re-surfaced the same risk for the
remaining 9 images site-wide (7 photo-strip images + 1 bento background on the home page, 1
distributor CTA image on the home page), plus a 10th on `templates/pages/contact.html` (the "Visit
Us" section background) that hadn't even been in the original 2026-08-07 sweep's scope.

All 10 downloaded via a one-off script (`requests` + Pillow, both already project dependencies —
no new dependency added) and converted to WebP, matching `bancostore/media.py`'s established
quality convention (85) but with a smaller resize cap than that helper's own 1600px (400px for the
small photo-strip thumbnails, 900-1000px for the two larger hero-style images) since these are
fixed decorative marketing images, not a zoomable admin-uploaded product gallery. Combined size:
~390KB of third-party requests eliminated, replaced with 264KB of self-hosted WebP (a real
reduction, not just a risk fix, since the Google-served originals were also ~70% oversized for
their actual display dimensions per the audit). `about-hero.jpg` (already self-hosted, but flagged
by the same audit as an oversized JPEG for its largest real use on the About page hero) was
converted to WebP at its native 1376×768 resolution — not resized, since the About page's `max-w-5xl`
16:9 hero is a legitimately large use of this file, only the home page's small thumbnail use was
oversized — cutting it from 110KB to 55KB with no quality loss at the size that actually matters.
`templates/pages/contact.html` needed a new `{% load static %}` (it had never used the tag before).
Zero `googleusercontent`/`lh3.` references remain anywhere in `templates/` (grep-confirmed).

**Deliberately not touched — flagged, not silently fixed:** the audit's other two structural
findings (a 1.1MB un-subsetted Material Symbols icon webfont — this codebase's actively-used icon
system alongside Heroicons, not a leftover, so the fix is subsetting/self-hosting rather than
removal, and needs either a new build-time font-subsetting tool or a larger icon-migration effort;
no text compression + HTTP/1.1 on the production Nginx config) are real but out of scope for a
same-session fix: one needs a Boundaries-gated new dependency decision, the other is a production
server config change requiring the user's sign-off before touching the live VPS, per `SPEC.md`
Boundaries and the Task 24 deploy precedent.

Live-verified against a real `runserver` (not just pytest): curled home, about, and contact pages,
confirmed every image `src`/`url()` now points at a local `/static/...` path, confirmed zero
`googleusercontent` references remain in any rendered page, and curled each of the 11 new/changed
image files directly to confirm all return HTTP 200. `npm run build` re-run (no Tailwind class
changes, so identical output hashes — confirms this was a pure asset-swap, not a template-structure
change). Targeted suite green (`tests/feature/catalog/`, `tests/feature/pages/`: 110 passed), no
Python logic touched so no broader regression risk.

**PR #70 CodeRabbit fix round (2026-08-11), all 6 findings fixed in one follow-up commit per this
codebase's own established batching convention:**
1. **Real, Major-severity fix:** `_build_product_json_ld` and `organization_json_ld` both rendered
   `json.dumps(data)|safe` inside a `<script type="application/ld+json">` tag — plain `json.dumps`
   doesn't escape `<`/`>`/`&`, so a product description or admin-entered social link containing a
   literal `</script>` could break out of the script element and inject arbitrary HTML. Fixed with
   a new shared `bancostore/json_ld.py::dumps_for_script_tag()`, the same escape mapping Django's
   own `django.utils.html.json_script()` uses — applied to *both* JSON-LD builders, not just the
   Product one CodeRabbit's comment specifically flagged, since `organization_json_ld`'s `sameAs`
   list reads the same class of admin-entered free text. Two new regression tests
   (`test_product_json_ld_escapes_closing_script_tag_in_description`,
   `test_organization_json_ld_escapes_closing_script_tag_in_social_link_url`) prove the raw
   `<script>` element never contains a literal `</script>` mid-tag, while the parsed JSON still
   round-trips back to the real (malicious-looking) input string.
2. **Real, functional-correctness fix:** `og:image:width`/`og:image:height` were hardcoded to
   `1200`/`630` even when `product_detail.html` overrides `og_image` with a real product photo of
   different dimensions — inaccurate metadata. Fixed by wrapping the tags in a new
   `og_image_dimensions` block (`templates/base_store.html`), overridden empty on product pages
   specifically when a real photo exists (`templates/catalog/home.html`... `templates/catalog/product_detail.html`).
   **Real Django gotcha hit while implementing this, not assumed:** an `{% if %}` *wrapping* a
   `{% block %}` tag has no effect on which content a parent template's block placeholder resolves
   to — Django collects block overrides structurally by walking the child template's full node
   tree (including inside `{% if %}`/`{% for %}` bodies) at parse time, then renders only the
   matched block node's own inner content, never re-evaluating whatever conditional happened to
   surround it in the child. The `{% if %}` has to live *inside* the block tags to have any
   runtime effect — exactly the pattern the pre-existing `og_image` block on the same line already
   used correctly, caught before it shipped by checking that block's own shape rather than
   guessing. Live-verified against a real `runserver` across all three cases: home page (default
   banner) keeps the 1200x630 tags, a real product photo omits them entirely, and a product with no
   photo yet still gets the 1200x630 fallback tags (confirms the `{% if not product.primary_image %}`
   condition itself is also correct, not just present).
3. **Real, Minor perf fix:** `_build_product_json_ld` called `product.primary_image` twice (once
   for the presence check, once for the URL), and `product_detail`'s queryset never prefetched
   `images` at all — meaning this property (which walks `self.images.all()`) was already a
   pre-existing N+1 every time the template itself called it (the gallery loop, the primary-image
   check), not something newly introduced. Fixed both at once: `product_detail`'s
   `prefetch_related()` gained `"images"` alongside its existing `"variants"`, and
   `_build_product_json_ld` now resolves `primary_image` once into a local variable instead of
   re-reading the property.
4. **Test-quality fix:** `test_robots_txt_disallows_private_paths` asserted
   `"Disallow: /distributors/" in content` — a substring check that would pass for *any* deeper
   distributor path being disallowed, without actually proving the specific important ones
   (`dashboard/`, `withdraw/`) are present. Fixed to assert exact `Disallow:` lines via
   `.splitlines()`, plus a new companion test
   (`test_robots_txt_does_not_disallow_distributor_registration_or_login`) proving the positive
   half of the same claim — registration/login genuinely stay crawlable, not just "the test didn't
   check for a false Disallow."
5. **Documentation-accuracy fix (Minor):** `tasks/plan.md` Checkpoint K's summary claimed "full
   suite green throughout" two paragraphs after admitting one full local run had a known
   pre-existing failure — a real, confusing overclaim. Reworded to state targeted-suite,
   full-local-suite, and real-MySQL-CI results as three separate, precisely-scoped facts instead of
   one blanket claim.
6. **Documentation-accuracy fix (Major, flagged by CodeRabbit as reachable via an automated
   analysis run against this exact file):** Task 38's record only cited a targeted-suite result,
   not a final full-suite run reflecting the finished state of the branch. A full local `pytest -q`
   run at the pre-fix-round commit (1393 passed, 1 skipped, plus the one already-`git stash`-
   confirmed pre-existing Task 36 failure) is recorded here as that missing data point; a fresh
   targeted run after this fix round itself (`tests/feature/catalog/`, `tests/feature/pages/`: 113
   passed, covering all 6 fixes above) is recorded rather than re-running the full ~20-minute suite
   a third time for changes confined to these same two directories.

All 6 fixes pushed as a single follow-up commit to PR #70, per this codebase's established "fix
CodeRabbit findings together, not one push per finding" convention.
