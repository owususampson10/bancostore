import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import RequestFactory, override_settings

from allauth.account.adapter import get_adapter

BASE_DIR = Path(__file__).resolve().parents[3]


def _production_settings():
    """Imports bancostore/settings.py the way production does. The production
    block is gated on `not DEBUG and not _RUNNING_UNDER_PYTEST`, so it can
    never load inside this test process -- a fresh interpreter with DEBUG=False
    and no PYTEST_VERSION is the only way to read what production really gets."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_VERSION"}
    env.update(
        DEBUG="False",
        SECRET_KEY="test-only-not-the-insecure-placeholder-0123456789",
        ALLOWED_HOSTS="bancostore.com",
    )
    script = (
        "import json, bancostore.settings as s; print(json.dumps({"
        "'ratelimit': getattr(s, 'RATELIMIT_IP_META_KEY', None), "
        "'allauth': getattr(s, 'ALLAUTH_TRUSTED_CLIENT_IP_HEADER', None), "
        "'allauth_proxy_count': getattr(s, 'ALLAUTH_TRUSTED_PROXY_COUNT', 0)}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_allauth_and_django_ratelimit_trust_the_same_nginx_header_in_production():
    """Task 55d: django-allauth 65.14.2+ ignores proxy headers by default, so
    behind Nginx every visitor's IP would be Nginx's own 127.0.0.1 and
    allauth's login/signup/reset rate limits would be one global bucket.
    deploy/nginx/bancostore.conf sets X-Real-IP to $remote_addr (overwriting
    anything the visitor sent), and django-ratelimit already trusts exactly
    that header (Task 24d, spoof-tested live). allauth must trust the same one,
    and only that one -- X-Forwarded-For is appended to, not overwritten."""
    settings = _production_settings()

    assert settings["ratelimit"] == "HTTP_X_REAL_IP"
    assert settings["allauth"] == "X-Real-IP"
    assert settings["allauth_proxy_count"] == 0


@override_settings(ALLAUTH_TRUSTED_CLIENT_IP_HEADER="X-Real-IP")
def test_allauth_client_ip_comes_from_nginx_header_not_a_forged_forwarded_for():
    """With the production value, two different visitors behind the same Nginx
    resolve to their own IPs even when both send an identical forged
    X-Forwarded-For -- per-visitor rate limiting, not one shared bucket."""
    factory = RequestFactory()
    forged = "203.0.113.99"

    visitor_a = factory.get(
        "/",
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REAL_IP="198.51.100.7",
        HTTP_X_FORWARDED_FOR=forged,
    )
    visitor_b = factory.get(
        "/",
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REAL_IP="198.51.100.8",
        HTTP_X_FORWARDED_FOR=forged,
    )

    assert get_adapter().get_client_ip(visitor_a) == "198.51.100.7"
    assert get_adapter().get_client_ip(visitor_b) == "198.51.100.8"
