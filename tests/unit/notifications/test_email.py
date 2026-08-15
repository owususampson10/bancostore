from django.conf import settings

import pytest
from constance import config

from apps.notifications.email import get_sender_email


@pytest.mark.django_db
def test_falls_back_to_the_server_default_when_blank():
    """The default -- blank -- must resolve to the already-validated
    settings.DEFAULT_FROM_EMAIL (Task 24d's own startup-time validation
    against known-insecure placeholder values), not an empty string."""
    config.SENDER_EMAIL_ADDRESS = ""

    assert get_sender_email() == settings.DEFAULT_FROM_EMAIL


@pytest.mark.django_db
def test_uses_the_live_admin_configured_sender_when_set():
    config.SENDER_EMAIL_ADDRESS = "orders@bancostore.com"

    assert get_sender_email() == "orders@bancostore.com"
