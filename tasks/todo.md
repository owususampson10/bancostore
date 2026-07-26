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
- [ ] **Several constance settings are decorative — they exist and look live in the admin panel but
  nothing reads them**: `MIN_PASSWORD_LENGTH`, `PASSWORD_COMPLEXITY_ENABLED`,
  `SESSION_TIMEOUT_MINUTES`, `ADMIN_SESSION_TIMEOUT_MINUTES`, `PASSWORD_RESET_EXPIRY_MINUTES`
  (allauth actually uses Django's own `PASSWORD_RESET_TIMEOUT`, unset, so it defaults to 3 days
  regardless of what the panel says), `ADMIN_2FA_METHOD` (default value `"sms"` is actively wrong
  since only authenticator-app 2FA is implemented), `GOOGLE_LOGIN_CUSTOMERS_ENABLED` /
  `GOOGLE_LOGIN_DISTRIBUTORS_ENABLED` (Google button visibility is actually driven by whether a
  `SocialApp` row exists, not this flag). Either wire these into real enforcement or mark them
  "not yet enforced" in the fieldset help text.
- [x] ~~`seed_roles` has no guard against running in a non-DEBUG environment~~ — **Fixed
  2026-07-12.** Raises `CommandError` outside `DEBUG`. Existing tests updated to force
  `settings.DEBUG = True` (matching CI's deliberate `DEBUG=False`), new test confirms the guard
  actually blocks the command and creates no stub account when `DEBUG=False`.
- [x] ~~OTP codes compared with `!=` instead of `secrets.compare_digest()`~~ — **Fixed 2026-07-12**,
  bundled with the OTP concurrency fix below since it touched the same line.
- [ ] No production security headers configured yet (`SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`) — not exploitable until
  something is actually deployed (Task 24), but should be added as an `if not DEBUG:` block before
  go-live rather than forgotten. **Fix together with the proxy-IP-trust item below** — both need
  the exact same "trust exactly one hop from Nginx" care and are easy to get subtly wrong
  (`SECURE_PROXY_SSL_HEADER` trusting a header an external client can also set is the same class of
  mistake as the rate-limit IP key doing the same).
- [ ] `DEFAULT_FROM_EMAIL` uses the reserved `.test` TLD — fine for dev, must be swapped to a real
  deliverable domain (with SPF/DKIM) before production or reset/lockout emails may bounce or land
  in spam.

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
- [ ] **Rate limiting (and the future `SECURE_PROXY_SSL_HEADER` header work above) will collapse
  into a single shared bucket — or become spoofable — once this sits behind Hostinger's Nginx.**
  `key="ip"` resolves via `request.META['REMOTE_ADDR']`, which is identical for every visitor once
  a reverse proxy sits in front (Nginx's own connection, not the real client's). Two failure modes:
  everyone shares one rate-limit bucket (a single confused user could trip it for the whole site),
  or — if `X-Forwarded-For` is ever naively trusted without restricting to exactly one hop from
  Nginx — an attacker can spoof a fresh IP per request and bypass rate limiting entirely. Needs the
  real Nginx config to fix correctly (set `X-Real-IP`/`X-Forwarded-For` in Nginx, then configure
  `RATELIMIT_IP_META_KEY` to trust exactly that one hop) — tracked for Task 24, not guessed at now.
- [x] ~~`PAYSTACK_SECRET_KEY` (and the rest of the payment-gateway constance settings) will be
  stored in plaintext in the database~~ — **Fixed 2026-07-13**, when Paystack work actually
  started (Task 10b), per this note's own instruction not to defer it. `PAYSTACK_PUBLIC_KEY` /
  `PAYSTACK_SECRET_KEY` moved to environment variables (`bancostore/settings.py`, matching
  `MNOTIFY_API_KEY`/`EMAIL_HOST_PASSWORD`) instead of `django-constance`. The rest of
  `PAYMENT_GATEWAY_SETTINGS` (channels, mode toggle, copy) stays in constance — legitimate
  business-rule config, not secrets.
- [ ] `SESSION_ENGINE = "django.contrib.sessions.backends.cache"` has no DB fallback
  (`cached_db`) — any Redis eviction/restart logs out every user platform-wide, including admin's
  mandatory-2FA state. Worth a documented mitigation before go-live.
- [x] ~~Distributor registration reveals phone-number existence (`apps/distributors/forms.py`'s
  uniqueness check)~~ — **Fixed 2026-07-13** as part of Task 10a, for an unrelated reason (payment
  gating the account, not this issue specifically): `clean_phone_number` no longer checks the
  `Distributor` table at all. It only checks `PendingRegistration` (to avoid an unhandled
  `IntegrityError` on a duplicate submission), which doesn't reveal whether a phone number belongs
  to a real, existing distributor.
- [ ] CI hygiene, not urgent: `.github/workflows/ci.yml` has no explicit `permissions:` block
  (defaults to broader `GITHUB_TOKEN` scope than needed), `actions/checkout@v4` is pinned to a
  mutable tag rather than a commit SHA, and there's no dependency vulnerability scan step
  (`pip-audit`/`safety`) yet — cheap to add now while the dependency set is still small.

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
- [ ] Filtering by status/date/customer returns the correct, correctly-paginated set
- [ ] The PDF invoice contains exactly the fields ADR-0006 decision 7 lists, for any order
- [ ] Cancel/refund/status-update actions call 18b/18c's functions, never duplicate their logic
- [ ] A staff account without the relevant Django permission cannot trigger a cancel/refund action (mirroring Task 15/16's own permission-lockdown precedent)

**Verification:**
- [ ] pytest test: filter combinations return the expected order set
- [ ] pytest test: PDF invoice generation succeeds and contains the expected fields for a real order
- [ ] pytest test: a permission-lacking staff account is blocked from the cancel/refund action

**Dependencies:** 18b, 18c, 18d merged

**Files likely touched:** `apps/orders/views.py` or `apps/admin_portal/views.py`, `apps/orders/services.py` (PDF rendering), `tests/feature/orders/test_order_management_backend.py`

**Estimated scope:** M

**Skills:**
- *Before:* `source-driven-development` (confirming WeasyPrint's real HTML-to-PDF API against its own docs before use)
- *During:* `test-driven-development`, `incremental-implementation`, `security-and-hardening` (the admin-action permission lockdown specifically)
- *After:* `code-review-and-quality`

---

#### Task 18f: Admin order management -- frontend (Stitch-designed `admin_portal` page)

**Description:** Real Stitch-designed UI for the order management page, matching every prior admin
screen (KYC Review Queue, Withdrawal Review Queue, Distributor Directory, Commission Cycle Detail).
**Claude does not design this UI freehand** -- the user sends a Stitch prompt and shares the
resulting screens/export back for template integration, same as every prior page in this project.

**Acceptance criteria:**
- [ ] The order list, filters, and per-order detail (status update, tracking note, cancel/refund, PDF invoice link) render the real Stitch design, integrated with 18e's actual data
- [ ] Verified across the achievable real-browser breakpoints (1440/1024/768px, and whatever floor the local OS's window-resize permits — see Task 17e's own note on the ~500px practical floor and why true 320px needs a code-inspection fallback, not a fabricated screenshot)

**Verification:**
- [ ] Manual check: an admin filters, updates status, adds a tracking note, cancels a confirmed order (stock/PV visibly reversed in the DB), and downloads a PDF invoice, all via the real UI in a real browser

**Dependencies:** 18e merged; Stitch prompt sent and screens received from the user

**Files likely touched:** `templates/admin_portal/order_management.html` (new), `templates/admin_portal/partials/order_*.html`

**Estimated scope:** M

**Skills:**
- *During:* `frontend-ui-engineering`, `incremental-implementation`
- *After:* `code-review-and-quality`, `browser-testing-with-devtools`

---

#### Task 18g: Full-suite verification, CI, PR, Checkpoint G (closing)

**Description:** Same branch -> PR -> CI (real MySQL) -> CodeRabbit -> merge workflow as every
prior task. Closes the "order status updates correctly with notifications" portion of Checkpoint G
that Task 17f explicitly left open for this task.

**Acceptance criteria:**
- [ ] Full pytest suite green, including every new orders test from 18a-18f
- [ ] `black`/`ruff` clean, `manage.py check` clean
- [ ] CI green against real MySQL, not just local SQLite
- [ ] CodeRabbit review complete, actionable findings resolved or explicitly deferred with reasoning

**Verification:**
- [ ] Checkpoint G's remaining portion passes end-to-end in a real browser session: an order moves through its full lifecycle with a notification at each step, and admin cancellation of a confirmed order visibly reverses stock/PV in the database, not just pytest

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

**Dependencies:** Task 10b, Task 10c, Task 10d, Task 12

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
- [ ] Tree view renders the distributor's downline correctly using Task 9a's closure table
- [ ] Notification bell updates live via Django Channels for each event type listed in `SPEC.md` 6.6

**Verification:**
- [ ] pytest test: tree view shows correct nodes for a seeded downline
- [ ] pytest test: each notification type is dispatched on its triggering event

**Dependencies:** Task 9a, Task 20

**Files likely touched:** `apps/distributors/views.py` (tree view), `apps/notifications/models.py`, `tests/feature/distributors/test_notifications.py`

**Estimated scope:** M

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
commission oversight (Task 23, sliced further when started).

### Task 22: Admin Portal — KYC review screen

**Description:** Replaces `DistributorAdmin`'s Django-Admin KYC review (list + `DiditVerificationInline`
+ approve/reject bulk actions) with a Stitch-designed queue screen in `apps/admin_portal/`. Calls
the existing `apps.distributors.services.approve_kyc`/`reject_kyc` directly — no service-layer
changes, presentation only.

**Acceptance criteria:**
- [ ] Admin sees a queue of pending-KYC distributors with Didit's verification result summary
      (status, face-match/liveness scores, extracted name/document number, warnings, the three
      images) — same data `DiditVerificationInline` already shows, restyled
- [ ] Approve/reject work per-distributor (reject requires a reason, matching the existing
      Django-Admin confirmation-page pattern's requirement)
- [ ] Django Admin's own KYC screen keeps working unchanged (not removed, just no longer the
      admin's primary path)

**Verification:**
- [ ] pytest test: approve/reject from the new screen produce identical `Distributor.kyc_status`/
      `kyc_rejection_reason` results as the existing Django-Admin action (same service call)
- [ ] Live browser check: full approve and full reject flow against a real pending KYC record

**Dependencies:** Task 11 (KYC backend), admin login/2FA (Task 6)

**Files likely touched:** new `apps/admin_portal/` app (urls, views, templates), `templates/admin_portal/base_dashboard.html`, `templates/admin_portal/kyc_review.html`, `tests/feature/admin_portal/test_kyc_review.py`

**Estimated scope:** M

---

### Task 23: Admin Portal — withdrawal approval, distributor management, commission oversight

**Description:** Absorbs the original Task 22/23 scope (distributor search/profile/suspend,
commission oversight) plus a Stitch-designed withdrawal approval screen fronting Task 16d's
already-built `approve_withdrawal_request`/`reject_withdrawal_request`. To be sliced into
23a/23b/23c (mirroring Task 16a-16h) when started.

**Dependencies:** Task 22 (shared `apps/admin_portal/` shell), Task 16d (withdrawal backend), Task 13 (commission data)

**Estimated scope:** L — slice before starting

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
