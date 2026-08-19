import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone

import pytest
from constance import config
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.forms import AdminAuthenticationForm
from apps.accounts.models import AdminProfile

User = get_user_model()

ADMIN_URL = "/admin/"


def _create_admin(email="admin@example.test", password="AdminPassw0rd!"):
    return User.objects.create_user(
        username=email, email=email, password=password, is_staff=True
    )


def _verify_otp_in_session(client, user):
    """Mirrors what two_factor's login wizard does on successful token
    verification — mark this test client's session as OTP-verified for a
    confirmed device, without having to drive the full formtools wizard."""
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return device


@pytest.mark.django_db
def test_admin_login_with_email_and_password_works():
    from django.contrib.auth import authenticate

    _create_admin()
    user = authenticate(username="admin@example.test", password="AdminPassw0rd!")

    assert user is not None
    assert user.email == "admin@example.test"


@pytest.mark.django_db
def test_staff_user_without_verified_2fa_cannot_reach_admin_panel(client):
    user = _create_admin()
    client.force_login(user)

    response = client.get(ADMIN_URL)

    assert response.status_code != 200


@pytest.mark.django_db
def test_staff_user_with_verified_2fa_can_reach_admin_panel(client):
    user = _create_admin()
    client.force_login(user)
    _verify_otp_in_session(client, user)

    response = client.get(ADMIN_URL)

    assert response.status_code == 200


@pytest.mark.django_db
def test_2fa_requirement_cannot_be_bypassed_via_settings_toggle(client):
    """Regression guard: ADMIN_2FA_ENABLED must never actually gate the
    enforcement in code — per SPEC.md Boundaries, 2FA can never be disabled,
    even temporarily."""
    config.ADMIN_2FA_ENABLED = False
    user = _create_admin()
    client.force_login(user)

    response = client.get(ADMIN_URL)

    assert response.status_code != 200


@pytest.mark.django_db
def test_n_failed_admin_logins_locks_the_account(client):
    from django.contrib.auth import authenticate

    _create_admin()

    for _ in range(config.MAX_FAILED_LOGIN_ATTEMPTS):
        user = authenticate(username="admin@example.test", password="WrongPassword!")
        assert user is None

    profile = AdminProfile.objects.get(user__email="admin@example.test")
    assert profile.locked_until is not None
    assert profile.locked_until > timezone.now()

    # Even the correct password is rejected while locked.
    user = authenticate(username="admin@example.test", password="AdminPassw0rd!")
    assert user is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_failed_logins_do_not_lose_increments():
    """Regression test: lock_admin_after_repeated_failures used to read
    profile.failed_login_attempts, increment in memory, and save — a
    classic lost-update race under real concurrent failed logins. Fire
    MAX_FAILED_LOGIN_ATTEMPTS failed logins from separate threads/
    connections simultaneously and confirm the account still ends up
    locked with the exact right count, not undercounted."""
    from django.contrib.auth import authenticate
    from django.db import connection

    _create_admin()

    def attempt():
        try:
            authenticate(username="admin@example.test", password="WrongPassword!")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=attempt)
        for _ in range(config.MAX_FAILED_LOGIN_ATTEMPTS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    profile = AdminProfile.objects.get(user__email="admin@example.test")
    assert profile.failed_login_attempts == config.MAX_FAILED_LOGIN_ATTEMPTS
    assert profile.locked_until is not None
    assert profile.locked_until > timezone.now()


@pytest.mark.django_db
def test_lockout_holds_even_when_username_equals_email():
    """Regression test: `manage.py createsuperuser` naturally produces an
    account whose username equals its email if the operator types the same
    value at both prompts — a very ordinary setup. AUTHENTICATION_BACKENDS
    previously listed the stock ModelBackend, which matches on `username`
    and has no notion of AdminProfile.locked_until, so it authenticated a
    locked-out account before the lockout-aware EmailBackend ever got a
    chance to run. authenticate() stops at the first backend that returns a
    user, so this bypassed lockout entirely for any account shaped this
    way."""
    from django.contrib.auth import authenticate

    User.objects.create_user(
        username="admin@example.test",
        email="admin@example.test",
        password="AdminPassw0rd!",
        is_staff=True,
    )
    AdminProfile.objects.create(
        user=User.objects.get(email="admin@example.test"),
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() + timedelta(minutes=30),
    )

    user = authenticate(username="admin@example.test", password="AdminPassw0rd!")

    assert user is None


@pytest.mark.django_db
def test_lockout_sends_an_alert_email(settings):
    from django.contrib.auth import authenticate

    settings.DEFAULT_FROM_EMAIL = "no-reply@bancostore.test"
    config.LOCKOUT_ALERT_EMAIL = "security@bancostore.test"
    _create_admin()

    for _ in range(config.MAX_FAILED_LOGIN_ATTEMPTS):
        authenticate(username="admin@example.test", password="WrongPassword!")

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["security@bancostore.test"]
    assert "admin@example.test" in mail.outbox[0].body


@pytest.mark.django_db
def test_successful_login_resets_the_failed_attempt_counter():
    from django.contrib.auth import authenticate
    from django.test import RequestFactory

    from apps.accounts.signals import reset_admin_failed_attempts

    user = _create_admin()
    authenticate(username="admin@example.test", password="WrongPassword!")
    profile = AdminProfile.objects.get(user=user)
    assert profile.failed_login_attempts == 1

    # Simulate the user_logged_in signal firing, same as a real successful
    # login through the two-factor wizard would trigger.
    reset_admin_failed_attempts(
        sender=User, user=user, request=RequestFactory().get("/")
    )

    profile.refresh_from_db()
    assert profile.failed_login_attempts == 0


@pytest.mark.django_db
def test_authentication_succeeds_again_once_the_lock_window_has_passed():
    from django.contrib.auth import authenticate

    _create_admin()
    AdminProfile.objects.create(
        user=User.objects.get(email="admin@example.test"),
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() - timedelta(seconds=1),
    )

    # The backend's job is just "can this credential authenticate right now"
    # — an expired lock must not keep blocking a correct password.
    result = authenticate(username="admin@example.test", password="AdminPassw0rd!")

    assert result is not None


@pytest.mark.django_db
def test_completing_login_clears_a_previously_expired_lock(client):
    user = _create_admin()
    AdminProfile.objects.create(
        user=user,
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() - timedelta(seconds=1),
    )

    logged_in = client.login(username="admin@example.test", password="AdminPassw0rd!")

    assert logged_in is True
    profile = AdminProfile.objects.get(user=user)
    assert profile.locked_until is None
    assert profile.failed_login_attempts == 0


@pytest.mark.django_db
def test_admin_authentication_form_flags_a_locked_account():
    """AdminAuthenticationForm.locked lets the login template distinguish a
    locked account from a plain wrong password (see
    templates/two_factor/core/login.html), since Django's authenticate()
    swallows the PermissionDenied that EmailBackend raises either way."""
    user = _create_admin()
    AdminProfile.objects.create(
        user=user,
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() + timedelta(minutes=30),
    )

    form = AdminAuthenticationForm(
        data={"username": "admin@example.test", "password": "AdminPassw0rd!"}
    )

    assert form.is_valid() is False
    assert form.locked is True


@pytest.mark.django_db
def test_wrong_password_against_a_locked_admin_does_not_reveal_lock_state():
    """Regression test: submitting a wrong password against an already-locked
    admin account must not report locked=True — otherwise an attacker who
    doesn't know the real password can still confirm the account is locked
    just by resubmitting any wrong guess, without ever needing the correct
    password."""
    user = _create_admin()
    AdminProfile.objects.create(
        user=user,
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() + timedelta(minutes=30),
    )

    form = AdminAuthenticationForm(
        data={"username": "admin@example.test", "password": "SomeWrongGuess!"}
    )

    assert form.is_valid() is False
    assert form.locked is False


@pytest.mark.django_db
def test_admin_authentication_form_does_not_flag_a_plain_wrong_password():
    _create_admin()

    form = AdminAuthenticationForm(
        data={"username": "admin@example.test", "password": "WrongPassword!"}
    )

    assert form.is_valid() is False
    assert form.locked is False


@pytest.mark.django_db
def test_admin_authentication_form_succeeds_for_a_correct_unlocked_login():
    _create_admin()

    form = AdminAuthenticationForm(
        data={"username": "admin@example.test", "password": "AdminPassw0rd!"}
    )

    assert form.is_valid() is True
    assert form.locked is False


@pytest.mark.django_db
def test_completing_the_full_wizard_redirects_to_the_admin_portal_dashboard(client):
    """Bug found 2026-07-23 verifying Task 22 live in a real browser:
    BaseLoginView.get_success_url() falls back to settings.
    LOGIN_REDIRECT_URL when there's no `next` param, and that setting was
    never set -- every real admin completing the wizard landed on
    Django's default /accounts/profile/, a 404. First fix landed on
    /admin/ instead, which was wrong in a different way -- caught live by
    the user seeing Django's raw unstyled backend for a few seconds
    before reaching the styled KYC review screen. Drives the actual
    wizard (auth step then token step) rather than the session-shortcut
    most other tests here use, since the bug is specifically in what
    happens at the end of that flow."""
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    auth_response = client.post(
        "/account/login/",
        {
            "admin_login_view-current_step": "auth",
            "auth-username": "admin@example.test",
            "auth-password": "AdminPassw0rd!",
        },
    )
    assert auth_response.status_code == 200  # re-renders wizard at 'token' step

    from django_otp.oath import totp

    token_response = client.post(
        "/account/login/",
        {
            "admin_login_view-current_step": "token",
            "token-otp_token": f"{totp(device.bin_key):06d}",
        },
        follow=True,
    )

    assert token_response.status_code == 200
    assert token_response.redirect_chain[-1][0] == "/admin-portal/"


@pytest.mark.django_db
def test_admin_login_is_rate_limited_per_ip(client):
    """Account-level lockout only protects one email at a time — an
    attacker can spray guesses across many different admin emails from one
    IP with no limit. Confirm the view itself throttles by IP."""
    responses = []
    for i in range(30):
        responses.append(
            client.post(
                "/account/login/",
                {
                    "admin_login_view-current_step": "auth",
                    "auth-username": f"admin{i}@example.test",
                    "auth-password": "WrongPassword!",
                },
            )
        )

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_email_backend_does_not_check_lock_state_before_the_password():
    """Regression test: EmailBackend.authenticate() used to check
    profile.locked_until BEFORE user.check_password() — a locked account
    would raise PermissionDenied (skipping the slow password-hash check
    entirely) for ANY submitted password, while an unlocked account always
    ran the full hash check. That's a measurable timing side-channel that
    leaks "this account is currently locked" to a caller who doesn't know
    the real password — reopening, one layer under the form, exactly what
    AdminAuthenticationForm.clean() was rewritten to close. A wrong
    password against a locked account must behave identically (return
    None, no exception) to a wrong password against an unlocked one."""
    from django.core.exceptions import PermissionDenied

    from apps.accounts.backends import EmailBackend

    user = _create_admin()
    AdminProfile.objects.create(
        user=user,
        failed_login_attempts=config.MAX_FAILED_LOGIN_ATTEMPTS,
        locked_until=timezone.now() + timedelta(minutes=30),
    )

    backend = EmailBackend()

    # Wrong password against a locked account: must NOT raise — that would
    # mean the lock check ran (and won) before the password check.
    result = backend.authenticate(
        request=None, username="admin@example.test", password="WrongPassword!"
    )
    assert result is None

    # Correct password against a locked account: only now should the lock
    # be enforced.
    with pytest.raises(PermissionDenied):
        backend.authenticate(
            request=None, username="admin@example.test", password="AdminPassw0rd!"
        )


def _post_auth_step(client, email, password):
    return client.post(
        "/account/login/",
        {
            "admin_login_view-current_step": "auth",
            "auth-username": email,
            "auth-password": password,
        },
    )


def _post_token_step(client, device, remember=False):
    from django_otp.oath import totp

    data = {
        "admin_login_view-current_step": "token",
        "token-otp_token": f"{totp(device.bin_key):06d}",
    }
    if remember:
        data["token-remember"] = "on"
    return client.post("/account/login/", data, follow=True)


@pytest.mark.django_db
def test_checking_remember_device_sets_a_remember_cookie(client, settings):
    """Task: "remember this device for 7 days" (user-approved, 2026-07-30)
    -- completing the wizard with the remember checkbox checked must set
    a signed remember-cookie so a later login from the same browser can
    skip the token step. Without TWO_FACTOR_REMEMBER_COOKIE_AGE set, the
    library's AuthenticationTokenForm never even adds the 'remember'
    field, so this also proves the setting itself is wired up."""
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
    _post_token_step(client, device, remember=True)

    remember_cookies = [
        name for name in client.cookies if name.startswith("remember-cookie_")
    ]
    assert len(remember_cookies) == 1


@pytest.mark.django_db
def test_a_remembered_device_skips_the_token_step_on_the_next_login(client, settings):
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
    _post_token_step(client, device, remember=True)

    # A second, fresh login attempt -- only the auth step, no token this
    # time -- must go straight through, since this exact browser (the
    # same `client`, which persists cookies across requests like a real
    # browser session) already proved it has the device.
    second_login = _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    assert second_login.status_code == 302
    assert second_login.url == "/admin-portal/"


@pytest.mark.django_db
def test_not_checking_remember_still_requires_the_token_step_next_time(
    client, settings
):
    """Regression guard distinguishing this feature from
    test_2fa_requirement_cannot_be_bypassed_via_settings_toggle's own
    guarantee: a browser that has NEVER been remembered (or explicitly
    declined) must always be asked for a fresh code -- remembering is
    opt-in per login, never a standing bypass."""
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
    _post_token_step(client, device, remember=False)

    second_login = _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    # Still mid-wizard at the token step -- a 302 straight to the admin
    # portal would mean the token step was wrongly skipped.
    assert second_login.status_code == 200
    assert second_login.context["wizard"]["steps"].current == "token"


@pytest.mark.django_db
def test_a_devices_remember_cookie_never_trusts_a_different_admin_account(
    client, settings
):
    """A remember-cookie is scoped to the specific (user, device) pair
    that set it (django-two-factor-auth's own get_remember_device_cookie
    signs both in). A second admin logging in from the same physical
    browser/cookie-jar must still be asked for a fresh code."""
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    first_admin = _create_admin(email="first@example.test")
    first_device = TOTPDevice.objects.create(
        user=first_admin, name="default", confirmed=True
    )
    _post_auth_step(client, "first@example.test", "AdminPassw0rd!")
    _post_token_step(client, first_device, remember=True)

    second_admin = _create_admin(email="second@example.test")
    TOTPDevice.objects.create(user=second_admin, name="default", confirmed=True)

    second_login = _post_auth_step(client, "second@example.test", "AdminPassw0rd!")

    assert second_login.status_code == 200
    assert second_login.context["wizard"]["steps"].current == "token"


@pytest.mark.django_db
def test_a_removed_device_is_no_longer_trusted_by_an_old_remember_cookie(
    client, settings
):
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
    _post_token_step(client, device, remember=True)

    device.delete()
    new_device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    second_login = _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    assert second_login.status_code == 200  # back to the token step
    assert second_login.context["wizard"]["steps"].current == "token"

    result = _post_token_step(client, new_device, remember=False)
    assert result.status_code == 200
    assert result.redirect_chain[-1][0] == "/admin-portal/"


@pytest.mark.django_db
def test_the_remember_checkbox_is_unchecked_by_default(client, settings):
    """Security finding (code-review pass): django-two-factor-auth's own
    AuthenticationTokenForm defines 'remember' with initial=True, which
    would render the checkbox pre-checked -- an opt-OUT 7-day 2FA skip
    on the highest-value account type in this system, not the opt-in
    the feature is meant to be. An admin who doesn't notice and uncheck
    it would get remembered without deciding to."""
    user = _create_admin()
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7

    response = _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    assert response.status_code == 200
    assert not response.context["wizard"]["form"].fields["remember"].initial


@pytest.mark.django_db
def test_a_remembered_devices_cookie_expires_after_seven_days(client, settings):
    """The entire point of this feature -- confirm the 7-day boundary is
    real, not just that the mechanism exists. two_factor.views.utils
    calls a module-level time.time() (confirmed by reading its source),
    so a plain stdlib mock.patch simulates time passing without needing
    a third-party time-travel library."""
    import time
    from unittest.mock import patch

    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    now = time.time()
    with patch("two_factor.views.utils.time.time", return_value=now):
        _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
        _post_token_step(client, device, remember=True)

    eight_days_later = now + 60 * 60 * 24 * 8
    with patch("two_factor.views.utils.time.time", return_value=eight_days_later):
        second_login = _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    assert second_login.status_code == 200  # back to requiring the token step
    assert second_login.context["wizard"]["steps"].current == "token"


@pytest.mark.django_db
def test_a_remembered_device_login_is_logged_for_the_audit_trail(
    client, settings, caplog
):
    """Matches this codebase's established convention of logging/
    auditing security-relevant admin events -- a login that skipped the
    OTP prompt via a remembered device is exactly that kind of event."""
    import logging

    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
    _post_token_step(client, device, remember=True)

    with caplog.at_level(logging.INFO, logger="apps.accounts.views"):
        _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")

    assert any("remembered device" in record.message for record in caplog.records)


@pytest.mark.django_db
def test_a_fresh_token_entry_login_is_not_logged_as_a_remembered_device(
    client, settings, caplog
):
    import logging

    settings.TWO_FACTOR_REMEMBER_COOKIE_AGE = 60 * 60 * 24 * 7
    user = _create_admin()
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    with caplog.at_level(logging.INFO, logger="apps.accounts.views"):
        _post_auth_step(client, "admin@example.test", "AdminPassw0rd!")
        _post_token_step(client, device, remember=False)

    assert not any("remembered device" in record.message for record in caplog.records)


def test_admin_login_logo_links_to_the_storefront_not_back_to_itself():
    """Regression test, found via user report: templates/base_admin_auth.html
    (the shared header for every 2FA/admin-auth screen) linked its logo to
    two_factor:login -- itself -- leaving no way at all to reach the
    public storefront from the admin login screen. Fixed to catalog:home,
    matching how every other real admin panel (GitHub, Stripe, AWS) links
    its logo to the public site rather than a dead end."""
    import re

    from django.template.loader import render_to_string
    from django.urls import reverse

    html = render_to_string("two_factor/core/login.html", {})

    match = re.search(r'<a href="([^"]+)" class="flex items-center gap-2">', html)
    assert match is not None
    assert match.group(1) == reverse("catalog:home")
    assert match.group(1) != reverse("two_factor:login")


def test_2fa_setup_complete_continue_button_links_to_the_real_admin_portal():
    """Regression test, found via the same user report as the logout and
    logo fixes: templates/two_factor/core/setup_complete.html's "Continue
    to Admin Panel" button -- shown once after a brand-new admin finishes
    mandatory TOTP setup -- linked to admin:index, Django's raw native
    admin dashboard, instead of admin_portal:dashboard, the real
    Bancostore Admin Portal every other admin_portal view already routes
    to."""
    from django.template.loader import render_to_string
    from django.urls import reverse

    html = render_to_string("two_factor/core/setup_complete.html", {})

    assert f'href="{reverse("admin_portal:dashboard")}"' in html
    assert reverse("admin:index") not in html


# --- admin_portal_permission_denied (handler403): a brand-new admin with
# zero confirmed OTP devices gets redirected to 2FA setup instead of a
# bare 403, without weakening the mandatory-2FA guarantee for anyone else.


@pytest.mark.django_db
def test_brand_new_admin_with_no_devices_is_redirected_to_2fa_setup(client):
    from django.urls import reverse

    user = _create_admin()
    client.force_login(user)

    response = client.get(reverse("admin_portal:dashboard"))

    assert response.status_code == 302
    assert response.url == reverse("two_factor:setup")


@pytest.mark.django_db
def test_brand_new_admin_hitting_the_raw_django_admin_gets_its_own_login_redirect(
    client,
):
    """A CodeRabbit review suggested testing that handler403's "admin"
    app_name branch (covering the raw, un-branded Django Admin, not just
    admin_portal) also redirects a brand-new admin to two_factor:setup --
    on the assumption that the raw admin raises PermissionDenied the same
    way admin_portal's views do. Checked directly against
    two_factor.admin.AdminSiteOTPRequiredMixin's real source: it does NOT
    -- has_permission() simply returns False for an unverified user, and
    Django's own AdminSite.admin_view() responds to that by calling this
    mixin's own login(), which redirects straight to the admin's own
    login page (redirect_to_login()) -- PermissionDenied is never raised,
    so handler403 is never even invoked for this exact path. This test
    documents the real, verified behavior instead of the incorrect
    assumption -- the "admin" app_name stays in handler403's set as
    defense-in-depth for any OTHER PermissionDenied a custom admin view
    might raise, but the raw admin's own index page isn't one of them."""
    user = _create_admin()
    client.force_login(user)

    response = client.get(ADMIN_URL)

    assert response.status_code == 302
    assert response.url.startswith("/admin/login/")


@pytest.mark.django_db
def test_admin_with_an_unconfirmed_device_is_still_redirected_to_setup(client):
    """Someone who started 2FA setup but never scanned/confirmed the code
    has, from this handler's point of view, zero USABLE devices -- they
    should be sent back to finish setup, not blocked."""
    from django.urls import reverse

    user = _create_admin()
    TOTPDevice.objects.create(user=user, name="default", confirmed=False)
    client.force_login(user)

    response = client.get(reverse("admin_portal:dashboard"))

    assert response.status_code == 302
    assert response.url == reverse("two_factor:setup")


@pytest.mark.django_db
def test_staff_with_a_confirmed_device_but_unverified_session_still_gets_a_real_403(
    client,
):
    """The security-critical negative case: an admin who already has a
    real, confirmed authenticator device must NOT be redirected around
    2FA just because this particular session hasn't completed a real
    challenge yet (e.g. force_login in a test, or a stale/tampered
    session in production) -- that would be an actual 2FA bypass. They
    must still see a real 403, exactly as before this fix."""
    from django.urls import reverse

    user = _create_admin()
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.force_login(user)  # deliberately NOT _verify_otp_in_session

    response = client.get(reverse("admin_portal:dashboard"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_non_staff_user_hitting_admin_portal_gets_a_normal_403_not_redirected(client):
    """The handler's is_staff condition must never fire for a completely
    unrelated account (e.g. a distributor or customer somehow hitting an
    admin_portal URL) -- they should see a normal 403, never be sent to
    the admin's own 2FA setup page."""
    from django.urls import reverse

    user = User.objects.create_user(
        username="+233241000099", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)

    response = client.get(reverse("admin_portal:dashboard"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_unrelated_permission_denied_elsewhere_is_never_redirected_to_2fa_setup(
    client,
):
    """Regression test for a real bug a fresh-context adversarial review
    caught: a first version of this handler checked only user state
    (is_staff/is_verified/no devices), with no check on which page raised
    PermissionDenied. apps/distributors/views.py::dashboard raises the
    exact same PermissionDenied for a completely unrelated reason (not
    being a distributor) -- an admin with incomplete 2FA setup hitting
    THAT page must see a normal 403, not get told to go set up an
    authenticator app, which has nothing to do with why they were
    actually denied."""
    from django.urls import reverse

    user = _create_admin()  # is_staff=True, zero OTP devices, not a distributor
    client.force_login(user)

    response = client.get(reverse("distributors:dashboard"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_with_verified_2fa_is_never_affected_by_this_handler(client):
    """Sanity check that the happy path (already covered by
    test_staff_user_with_verified_2fa_can_reach_admin_panel for the raw
    Django Admin) also still works for the branded admin_portal."""
    from django.urls import reverse

    user = _create_admin()
    client.force_login(user)
    _verify_otp_in_session(client, user)

    response = client.get(reverse("admin_portal:dashboard"))

    assert response.status_code == 200
