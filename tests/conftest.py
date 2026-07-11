from django.contrib.auth import get_user_model
from django.core.cache import cache

import pytest
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.notifications.sms import fake_outbox

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_django_cache():
    """django-constance is Redis-cached; pytest-django only rolls back the
    database between tests, so a value written to a constance setting in one
    test would otherwise leak into the next via Redis."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _clear_fake_sms_outbox():
    """fake_outbox is a plain module-level list (mirrors django.core.mail.outbox),
    so nothing resets it between tests automatically — same class of leak as the
    Redis-backed constance cache above."""
    fake_outbox.clear()
    yield
    fake_outbox.clear()


@pytest.fixture(autouse=True)
def _force_fake_sms_sender(settings):
    """Django's test runner automatically forces EMAIL_BACKEND to locmem
    regardless of what's in .env, so real Gmail sends never happen in tests —
    but there's no equivalent built-in protection for our own MNOTIFY_API_KEY
    setting. Once a real key is added to .env (Task 5's final verification
    step), every test run started silently hitting the real mNotify API
    instead of the fake sender. Force it empty here so tests can never spend
    real SMS credit, no matter what's configured locally."""
    settings.MNOTIFY_API_KEY = ""


@pytest.fixture
def staff_client(client, db):
    """A Django test client logged in as an is_staff user with a verified
    2FA session — everything AdminSiteOTPRequiredMixin needs to let a
    request through to Django Admin. Mirrors
    tests/feature/accounts/test_admin_auth.py's _verify_otp_in_session
    helper, pulled into a shared fixture since every admin-CRUD feature
    test (catalog, and later ones) needs the same setup."""
    user = User.objects.create_user(
        username="staff_tester",
        email="staff_tester@bancostore.test",
        password="StaffTester-Passw0rd!",
        is_staff=True,
        is_superuser=True,  # Bancostore admin is a single flat role (SPEC.md
        # "admin controls every business rule"), not a granular per-model
        # permission system, so every real admin account is a superuser.
    )
    client.force_login(user)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client
