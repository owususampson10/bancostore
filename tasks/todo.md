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
- [ ] **`PAYSTACK_SECRET_KEY` (and the rest of the payment-gateway constance settings) will be
  stored in plaintext in the database** once Paystack integration is built (still an open SPEC.md
  question) — `django-cryptography` is in `requirements.txt` specifically for this per SPEC.md Tech
  Stack, but isn't wired up anywhere yet. Not exploitable today (the field is empty, no Paystack
  code exists), but easy to ship silently once that work lands since the admin-panel scaffolding
  already looks "done." Add `CONSTANCE_ADDITIONAL_FIELDS` with an encrypted field type (or move it
  to an environment variable like `MNOTIFY_API_KEY`/`EMAIL_HOST_PASSWORD` already are) when Paystack
  work starts, not after.
- [ ] `SESSION_ENGINE = "django.contrib.sessions.backends.cache"` has no DB fallback
  (`cached_db`) — any Redis eviction/restart logs out every user platform-wide, including admin's
  mandatory-2FA state. Worth a documented mitigation before go-live.
- [ ] Distributor registration reveals phone-number existence (`apps/distributors/forms.py`'s
  uniqueness check) while password-reset deliberately doesn't — a minor, largely unavoidable
  enumeration inconsistency. Low priority; document as an accepted tradeoff or align both flows.
- [ ] CI hygiene, not urgent: `.github/workflows/ci.yml` has no explicit `permissions:` block
  (defaults to broader `GITHUB_TOKEN` scope than needed), `actions/checkout@v4` is pinned to a
  mutable tag rather than a commit SHA, and there's no dependency vulnerability scan step
  (`pip-audit`/`safety`) yet — cheap to add now while the dependency set is still small.

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
