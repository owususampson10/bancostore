from django.contrib.auth import get_user_model
from django.core.cache import cache

import pytest
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.notifications.sms import fake_outbox

User = get_user_model()

try:
    import weasyprint  # noqa: F401

    WEASYPRINT_AVAILABLE = True
except OSError:
    # WeasyPrint's own __init__ eagerly dlopen()s the system Pango
    # library at import time -- this raises OSError, not ImportError,
    # when Pango isn't present (source-driven-development, 2026-07-26;
    # see project_weasyprint_pango_blocked_locally memory). Computed
    # exactly ONCE here, in conftest.py (pytest always imports this
    # before collecting any test module), rather than once per test
    # file that needs it: Task 45 found a real, previously-latent bug
    # by adding a second independent `try: import weasyprint` site --
    # cffi's dlopen leaves corrupted internal C state after a first
    # failed attempt, and a SECOND independent import attempt in the
    # same process segfaults instead of cleanly re-raising OSError
    # (reproduced directly: `pytest tests/unit/test_exports.py
    # tests/feature/admin_portal/test_order_management.py` crashed the
    # whole interpreter; either file alone, or CI where Pango installs
    # cleanly via apt, does not). One canonical import site removes the
    # possibility regardless of test collection order.
    WEASYPRINT_AVAILABLE = False


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
def _isolate_media_root(settings, tmp_path):
    """Any test that creates a real ImageField/FileField (e.g.
    ProductImage) without this would write into the real project media/
    directory and never clean up -- CodeRabbit caught this on PR #53
    after Task 26's catalog tests started uploading real images.
    tmp_path is unique per test and pytest cleans it up automatically."""
    settings.MEDIA_ROOT = tmp_path


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
