# ADR-0012: Admin password reset design

## Status

Accepted (2026-08-18)

## Date

2026-08-18

## Context

The admin login page's "Forgot password?" link (`templates/two_factor/core/login.html`) was a
dead `href="#"` — found by the user while auditing the app for dead links. No admin password-reset
mechanism existed at all; the customer-facing flow already does email-link-based reset via
allauth's `account_reset_password`/`account_reset_password_from_key` views, shared machinery
across all three roles (customer/distributor/admin) since there is one `User` model.

Given admin is the highest-privilege account type in this system, this went through a
`doubt-driven-development` cycle (a fresh-context `security-auditor` agent) before implementation,
adversarially reviewing a proposed design of "just reuse allauth's existing flow as-is." The review
raised 11 findings across Critical/High/Medium/Low. Each was verified against this project's actual
code and the installed library versions' source (not assumed) before being acted on:

- **Confirmed already safe, no action needed:** a password reset auto-logging the user in
  (`ACCOUNT_LOGIN_ON_PASSWORD_RESET`) defaults to `False` and is unset here; even if it were `True`,
  every `apps.admin_portal` view and Django Admin itself gate on real OTP-verified status
  (`is_admin_portal_staff`/`AdminSiteOTPRequired`), not just "authenticated" — an unverified session
  from any source is rejected regardless. The remembered-device 2FA-skip cookie
  (`TWO_FACTOR_REMEMBER_COOKIE_AGE`, Task 4-6) is automatically invalidated the instant a password
  changes, because `django-two-factor-auth` signs that cookie over `user.password` itself
  (`two_factor/views/utils.py::hash_remember_device_cookie_value`) — confirmed by reading the
  library source directly, then locked in with
  `test_admin_password_reset_invalidates_remembered_device_cookie`. Email enumeration is already
  prevented by allauth's own `ACCOUNT_PREVENT_ENUMERATION` (defaults `True`, unmodified here). A
  seeded admin account with no verified `EmailAddress` row is still found correctly, because
  allauth's `filter_users_by_email` falls back to a direct `User.email` match when no verified
  `EmailAddress` exists. Rate limiting already exists — allauth's own `PasswordResetView.form_valid`
  calls `ratelimit.consume_or_429(..., action="reset_password", key=email)`, with a built-in default
  of `20/m/ip,5/m/key` (`ACCOUNT_RATE_LIMITS` is unset here, so the library default applies) — a
  real, adequate per-email cap already in place; adding a second, redundant rate limiter on top
  would have been unnecessary duplication.
- **Real gaps, fixed:** no audit log entry existed for a completed admin password reset, despite
  this codebase's established convention of logging security-relevant admin events
  (`AdminLoginView.done()`'s remembered-device log, KYC/IR ID history) — fixed via a
  `password_reset` signal receiver (Decision 4 below). The reset flow would have dropped an admin
  into allauth's customer-styled `base_auth.html` shell mid-flow, breaking this project's
  established "every admin-facing screen should look like the rest of the app" convention
  (Task 22/23/26/27/28) — fixed via dedicated admin-branded templates and views (Decision 1).

## Decision

### 1. A parallel, admin-branded set of views/templates/URLs, not a role branch inside the shared ones

`apps/accounts/views.py` adds `AdminPasswordResetView`, `AdminPasswordResetDoneView`,
`AdminPasswordResetFromKeyView`, `AdminPasswordResetFromKeyDoneView` — thin subclasses of allauth's
own `PasswordResetView`/`PasswordResetDoneView`/`PasswordResetFromKeyView`/
`PasswordResetFromKeyDoneView`, each only overriding `template_name` (and `form_class`/
`success_url` where relevant) to point at new `templates/account/admin_password_reset*.html`
templates extending `base_admin_auth.html` (the same chrome as the admin login/2FA screens), not
`base_auth.html` (customer). New URLs live under the existing `account/` (singular) admin auth
namespace already established by `admin_login`, distinct from allauth's `accounts/` (plural)
customer namespace: `account/password/reset/`, `account/password/reset/done/`,
`account/password/reset/key/<uidb36>-<key>/` (a `re_path`, mirroring allauth's own regex exactly —
see Decision 3), `account/password/reset/key/done/`.

All of the actually sensitive logic — email lookup, enumeration protection, token
generation/validation, the built-in rate limit, password validation, session invalidation on
password change (Django's own `get_session_auth_hash` mechanism) — is 100% inherited from allauth,
untouched. Only the URL/branding routing is duplicated, which is the smallest surface that could
satisfy the "every admin screen matches the app" requirement without touching shared customer code
paths.

### 2. `get_form_class()` must be overridden to bypass this project's global `ACCOUNT_FORMS` setting

Found by a failing test, not assumed: `PasswordResetView.get_form_class()` calls
`get_form_class(app_settings.FORMS, "reset_password", self.form_class)`, and `app_settings.FORMS`
is this project's own global `ACCOUNT_FORMS` setting (`bancostore/settings.py`), which already has
`"reset_password"`/`"reset_password_from_key"` keys pointing at `CustomerResetPasswordForm`/
`CustomerResetPasswordKeyForm`. `dict.get(key, default)` returns the dict's own value when the key
exists, completely ignoring the `default_form` argument — so `AdminPasswordResetView.form_class =
AdminResetPasswordForm` was silently discarded in favor of the customer form every time, and the
emailed reset link kept pointing at the customer confirm page even though the view's own attribute
said otherwise. Fixed by overriding `get_form_class()` on both `AdminPasswordResetView` and
`AdminPasswordResetFromKeyView` to `return self.form_class` directly, bypassing the global lookup.
`test_admin_password_reset_email_links_to_admin_branded_confirm_page` reproduced this exact failure
before the fix and passes after.

### 3. The emailed confirm-link routing is fixed in the form, not the adapter

allauth's `ResetPasswordForm._send_password_reset_mail` calls `get_adapter().get_reset_password_from_key_url(key)`
with **no request argument** at that call site — the adapter instance built there has
`self.request = None`, so there is no way to read "which front door initiated this" from adapter
state, and the default implementation hardcodes `reverse("account_reset_password_from_key", ...)`
by name with no override hook for just the URL name. `AdminResetPasswordForm` (`apps/accounts/forms.py`)
overrides `_send_password_reset_mail` directly instead — a ~30-line duplicate of allauth's own
method with the one line that matters changed to `reverse("admin_password_reset_from_key", ...)`.
This was judged an acceptable, targeted duplication (not the "second divergent password-reset code
path" the original design review warned against) because it duplicates URL-routing/branding logic
only, not any of the actual security-relevant token/validation logic.

The `admin_password_reset_from_key` URL itself is registered via `re_path`, not a plain `path()`
converter, mirroring allauth's own pattern
(`r"^password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$"`) exactly: `uidb36` is restricted to
alphanumeric characters specifically so the `uidb36-key` split can never be ambiguous when `key`
itself contains a literal `-` character, which a naive `path("<uidb36>-<key>/")` (both string
converters, both matching `[^/]+` greedily) would have gotten wrong under backtracking.

### 4. Audit logging via allauth's `password_reset` signal, not inside either new view

`apps/accounts/signals.py::log_admin_password_reset` connects to `allauth.account.signals.password_reset`
(fired by `finalize_password_reset` on every completed reset, customer or admin, regardless of
which URL/view triggered it) and logs only when `user.is_staff`. This is deliberately signal-based
rather than added inside `AdminPasswordResetFromKeyView.form_valid` — an admin could in principle
also complete a reset via the customer-facing `account_reset_password` page (nothing prevents
typing an admin's own email into that page; it isn't role-gated any more than
`admin_password_reset` is), and the audit convention this project already has ("log
security-relevant events for the account being affected") should hold regardless of which page was
used. Mirrors `AdminLoginView.done()`'s existing remembered-device log line exactly in shape and
intent.

## Alternatives Considered

### Point the admin link directly at allauth's existing `account_reset_password` (zero new code)

The simplest possible fix — reuses 100% existing, already-shipped machinery, no new views/templates/
URLs at all. Rejected per explicit user choice (`AskUserQuestion`): it would drop an admin into
customer-branded chrome (`base_auth.html`) mid-flow, breaking this project's own established
"every admin-facing screen looks like the rest of the app" convention that every other admin
screen (Task 22/23/26/27/28) was built to.

### Role-based branching inside allauth's own shared views (one URL, two templates picked by `user.is_staff`)

Would avoid registering parallel URLs. Rejected: the branding decision has to be made and baked
into the *emailed link* at request-submission time (which confirm page it points to), not just at
render time — a `user.is_staff`-branching shared view still needs the same "know which flavor to
use for the email link" problem Decision 3 solves, with the added complexity of one view class
juggling two template/URL identities instead of two small, readable subclasses.

### Two-factor-approval reset (a second existing admin must approve a reset request)

Discussed with the user directly (`AskUserQuestion`) as a stronger alternative to email-link reset.
Rejected in favor of the simpler, allauth-matching email-link flow: stronger security, but
meaningfully more friction and only works if the platform always has at least 2 active admins — not
guaranteed at this stage of the project.

## Consequences

- Two email lookup/token/rate-limit code paths now exist for `User` password resets — allauth's own
  (customer, `accounts/password/reset/...`) and this project's admin-branded subclasses
  (`account/password/reset/...`) — sharing the same underlying `ResetPasswordForm`/
  `ResetPasswordKeyForm` logic via inheritance, diverging only in template/URL-routing. A future
  allauth upgrade that changes `_send_password_reset_mail`'s internals would need
  `AdminResetPasswordForm._send_password_reset_mail` re-verified against the new version, since it's
  a duplicated (not wrapped) copy of that one method.
- An admin's password can still be reset via either front door (`admin_password_reset` or the
  customer-facing `account_reset_password`) — this is intentional (Decision 4), not a gap: the
  audit-logging and remember-cookie-invalidation guarantees hold regardless of which page was used,
  since both are properties of the underlying `User`/`password_reset` signal, not of either view.
- 9 new tests in `tests/feature/accounts/test_admin_auth.py` cover: the dead-link regression, a full
  request→email→confirm round trip, the emailed link resolving to the admin-branded confirm URL
  specifically, admin-branded chrome rendering, remembered-device-cookie invalidation (verified
  against the library's real signing behavior, not assumed), audit logging for an admin reset vs.
  no log entry for a customer reset, and non-enumeration for an unknown email.

### Post-implementation review findings (fixed pre-merge)

A `code-review-and-quality` pass and a parallel `security-and-hardening` pass, both run against the
actual committed diff (not just the design), independently surfaced the same two real gaps:

- **`AdminResetPasswordForm` had no `is_staff` gate.** Unlike `AdminAuthenticationForm.clean()`
  (which explicitly filters `is_staff=True`), the inherited `ResetPasswordForm.clean_email()` would
  happily find and email a real reset link to a customer or distributor who typed their own email on
  the admin-branded page — not a privilege-escalation risk (the token is bound to that one user, who
  can already reset their own password via the customer front door anyway), but a real mismatch with
  the admin-branded copy ("protect this admin account") and with Decision 1's stated intent that this
  is *the* admin-branded flow. Fixed by overriding `clean_email()` to filter `self.users` down to
  staff-only after the base lookup — the response is still identical either way (redirects to
  `admin_password_reset_done`, no enumeration oracle), but a non-staff email no longer actually
  receives a working reset link through this front door. Regression test:
  `test_admin_password_reset_does_not_email_a_non_staff_user`.
- **`_send_password_reset_mail`'s "mirrors exactly" claim was not quite true.** The duplicate had
  silently dropped one branch from allauth's real method (`if app_settings.AUTHENTICATION_METHOD !=
  AuthenticationMethod.EMAIL: context["username"] = user_username(user)`) — inert today only because
  `ACCOUNT_AUTHENTICATION_METHOD = "email"`, but a real, untested divergence risk if that setting
  ever changed. Restored for true parity with the original.
