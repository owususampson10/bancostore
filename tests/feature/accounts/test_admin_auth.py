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
        username="admin_user", email=email, password=password, is_staff=True
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
