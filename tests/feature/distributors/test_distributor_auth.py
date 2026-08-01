from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.notifications.otp import generate_otp
from apps.notifications.sms import fake_outbox

User = get_user_model()


def _create_unverified_distributor_with_registration_otp(
    phone_number="+233241234567", password="S3cure-Passw0rd!"
):
    """Task 10a decoupled account creation from OTP verification (accounts
    are no longer created by register() -- see tests/feature/distributors/
    test_registration_pending.py). OTP verification and login still need
    coverage on their own terms until Task 10b/11 decide where phone
    verification fits in the payment-gated sequence, so these tests set up
    an already-existing, unverified Distributor directly rather than going
    through register()."""
    user = User.objects.create_user(username=phone_number, password=password)
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    distributor = Distributor.objects.create(user=user, phone_number=phone_number)
    generate_otp(phone_number, purpose="registration")
    return distributor


@pytest.mark.django_db
def test_distributor_can_verify_otp_and_login(client):
    distributor = _create_unverified_distributor_with_registration_otp()
    session = client.session
    # str(...), matching exactly how the real views (login_view,
    # forgot_password) store it -- a raw PhoneNumber object was never
    # actually reachable in production, only in this test's own session
    # setup, and only "worked" before Task 30e's cached_db fix because
    # the old plain-cache session engine never JSON-serialized (it let
    # django_redis pickle it transparently).
    session["otp_phone_number"] = str(distributor.phone_number)
    session["otp_purpose"] = "registration"
    session.save()

    # A real OTP was "sent" via the fake sender — pull the actual code out of it
    # rather than reaching into the database, since that's what a real user does.
    assert fake_outbox, "no OTP SMS was sent"
    sent_message = fake_outbox[-1]["message"]
    code = "".join(ch for ch in sent_message if ch.isdigit())[:6]

    verify_response = client.post(reverse("distributors:verify_otp"), {"code": code})
    assert verify_response.status_code == 302

    distributor.refresh_from_db()
    assert distributor.phone_verified is True
    assert int(client.session["_auth_user_id"]) == distributor.user.id

    client.post(reverse("distributors:logout"))
    assert "_auth_user_id" not in client.session

    login_response = client.post(
        reverse("distributors:login"),
        {"phone_number": str(distributor.phone_number), "password": "S3cure-Passw0rd!"},
    )
    assert login_response.status_code == 302
    assert int(client.session["_auth_user_id"]) == distributor.user.id


@pytest.mark.django_db
def test_wrong_otp_does_not_verify_the_phone(client):
    distributor = _create_unverified_distributor_with_registration_otp()
    session = client.session
    session["otp_phone_number"] = str(distributor.phone_number)
    session["otp_purpose"] = "registration"
    session.save()

    response = client.post(reverse("distributors:verify_otp"), {"code": "000000"})

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.phone_verified is False


def _create_verified_distributor(phone_number="+233551234567", password="Passw0rd!"):
    user = User.objects.create_user(username=phone_number, password=password)
    return Distributor.objects.create(
        user=user, phone_number=phone_number, phone_verified=True
    )


@pytest.mark.django_db
def test_n_failed_logins_locks_the_account_for_the_configured_duration(client):
    distributor = _create_verified_distributor()

    for _ in range(config.MAX_FAILED_LOGIN_ATTEMPTS):
        response = client.post(
            reverse("distributors:login"),
            {"phone_number": "+233551234567", "password": "WrongPassword!"},
        )
        assert response.status_code == 200

    distributor.refresh_from_db()
    assert distributor.locked_until is not None
    assert distributor.locked_until > timezone.now()

    # Even the correct password is rejected while locked.
    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session

    # Once the lock window has passed, login works again.
    distributor.locked_until = timezone.now() - timedelta(seconds=1)
    distributor.save(update_fields=["locked_until"])

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    assert response.status_code == 302
    assert int(client.session["_auth_user_id"]) == distributor.user.id


@pytest.mark.django_db
def test_wrong_password_against_a_locked_account_does_not_reveal_lock_state():
    """Regression test: submitting a wrong password against an already-locked
    account must not report locked=True — otherwise an attacker who doesn't
    know the real password can still confirm the account is locked just by
    resubmitting any wrong guess, without ever needing the correct password."""
    from apps.distributors.services import attempt_distributor_login

    distributor = _create_verified_distributor()
    distributor.failed_login_attempts = config.MAX_FAILED_LOGIN_ATTEMPTS
    distributor.locked_until = timezone.now() + timedelta(minutes=30)
    distributor.save(update_fields=["failed_login_attempts", "locked_until"])

    result = attempt_distributor_login("+233551234567", "SomeWrongGuess!")

    assert result.success is False
    assert result.locked is False


@pytest.mark.django_db(transaction=True)
def test_concurrent_failed_logins_do_not_lose_increments():
    """Regression test: attempt_distributor_login used to read
    distributor.failed_login_attempts, increment in memory, and save — a
    classic lost-update race under real concurrent failed logins. Fire
    MAX_FAILED_LOGIN_ATTEMPTS failed logins from separate threads/
    connections simultaneously and confirm the account still ends up
    locked with the exact right count, not undercounted."""
    import threading

    from django.db import connection

    from apps.distributors.services import attempt_distributor_login

    _create_verified_distributor()

    def attempt():
        try:
            attempt_distributor_login("+233551234567", "WrongPassword!")
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

    distributor = Distributor.objects.get(phone_number="+233551234567")
    assert distributor.failed_login_attempts == config.MAX_FAILED_LOGIN_ATTEMPTS
    assert distributor.locked_until is not None
    assert distributor.locked_until > timezone.now()


@pytest.mark.django_db
def test_correct_password_against_a_locked_account_still_reports_locked():
    from apps.distributors.services import attempt_distributor_login

    distributor = _create_verified_distributor()
    distributor.failed_login_attempts = config.MAX_FAILED_LOGIN_ATTEMPTS
    distributor.locked_until = timezone.now() + timedelta(minutes=30)
    distributor.save(update_fields=["failed_login_attempts", "locked_until"])

    result = attempt_distributor_login("+233551234567", "Passw0rd!")

    assert result.success is False
    assert result.locked is True


@pytest.mark.django_db
def test_successful_login_resets_the_failed_attempt_counter(client):
    distributor = _create_verified_distributor()

    client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "WrongPassword!"},
    )
    distributor.refresh_from_db()
    assert distributor.failed_login_attempts == 1

    client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )
    distributor.refresh_from_db()
    assert distributor.failed_login_attempts == 0


@pytest.mark.django_db
def test_successful_login_does_not_redirect_back_to_the_login_page(client):
    """Regression test: a successful login used to redirect straight back to
    distributors:login, which just re-shows the empty login form — logged in
    but looking exactly like the login silently failed. Caught by the user
    manually testing and reporting 'nothing happened' after logging in."""
    _create_verified_distributor()

    response = client.post(
        reverse("distributors:login"),
        {"phone_number": "+233551234567", "password": "Passw0rd!"},
    )

    assert response.status_code == 302
    assert response.url != reverse("distributors:login")


@pytest.mark.django_db
def test_distributor_login_page_has_no_google_login_option(client):
    response = client.get(reverse("distributors:login"))

    # "google" alone would also match the Google Fonts <link> tags every page
    # uses — check specifically for a social-login button, not just the word.
    content = response.content.lower()
    assert b"continue with google" not in content
    assert b"provider_login_url" not in content
    assert b"/accounts/google/" not in content


@pytest.mark.django_db
def test_distributor_password_reset_via_otp(client):
    distributor = _create_verified_distributor()

    client.post(
        reverse("distributors:forgot_password"), {"phone_number": "+233551234567"}
    )

    assert fake_outbox
    sent_message = fake_outbox[-1]["message"]
    code = "".join(ch for ch in sent_message if ch.isdigit())[:6]

    verify_response = client.post(reverse("distributors:verify_otp"), {"code": code})
    assert verify_response.status_code == 302

    reset_response = client.post(
        reverse("distributors:set_new_password"),
        {"password1": "BrandNewPassw0rd!", "password2": "BrandNewPassw0rd!"},
    )
    assert reset_response.status_code == 302

    distributor.user.refresh_from_db()
    assert distributor.user.check_password("BrandNewPassw0rd!")


@pytest.mark.django_db
def test_resend_otp_is_rate_limited(client):
    """resend_otp sends a real SMS on every call with no throttle — an
    unlimited-resend endpoint is a direct SMS-cost DoS vector once a real
    MNOTIFY_API_KEY is live. Confirm it stops sending past a per-IP limit
    instead of accepting requests forever."""
    session = client.session
    session["otp_phone_number"] = "+233551234567"
    session["otp_purpose"] = "registration"
    session.save()

    responses = []
    for _ in range(10):
        # The real UI submits this as a form POST
        # (templates/distributors/verify_otp.html), not a bare GET.
        responses.append(client.post(reverse("distributors:resend_otp")))

    assert any(r.status_code == 429 for r in responses)
    # Not every one of the 10 requests actually sent an SMS once the limit
    # kicked in.
    assert len(fake_outbox) < 10


@pytest.mark.django_db
def test_resend_otp_rejects_get_and_does_not_send_sms(client):
    """resend_otp has a real side effect (a billed SMS send) but used to
    accept any HTTP method — a bare GET bypasses Django's CSRF check
    entirely (CSRF only applies to state-changing methods), so something
    as simple as an <img> tag could trigger a send while a victim had an
    in-progress OTP flow."""
    session = client.session
    session["otp_phone_number"] = "+233551234567"
    session["otp_purpose"] = "registration"
    session.save()

    response = client.get(reverse("distributors:resend_otp"))

    assert response.status_code == 405
    assert not fake_outbox


@pytest.mark.django_db
def test_verify_otp_is_rate_limited_per_ip(client):
    """verify_otp_view is where OTP guesses actually land — it had no
    throttle at all while its siblings (register, resend_otp,
    forgot_password, login) all did."""
    session = client.session
    session["otp_phone_number"] = "+233551234567"
    session["otp_purpose"] = "registration"
    session.save()

    responses = []
    for _ in range(30):
        responses.append(
            client.post(reverse("distributors:verify_otp"), {"code": "000000"})
        )

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_forgot_password_is_rate_limited_per_ip(client):
    _create_verified_distributor()

    responses = []
    for _ in range(10):
        responses.append(
            client.post(
                reverse("distributors:forgot_password"),
                {"phone_number": "+233551234567"},
            )
        )

    assert any(r.status_code == 429 for r in responses)


@pytest.mark.django_db
def test_distributor_login_is_rate_limited_per_ip(client):
    """Account-level lockout only protects one phone number at a time — an
    attacker can spray guesses across many different phone numbers from one
    IP with no limit. Confirm the view itself throttles by IP well before
    it would take to brute-force even a handful of distinct accounts."""
    responses = []
    for i in range(30):
        responses.append(
            client.post(
                reverse("distributors:login"),
                {
                    "phone_number": f"+23355000{i:04d}",
                    "password": "WrongPassword!",
                },
            )
        )

    assert any(r.status_code == 429 for r in responses)
