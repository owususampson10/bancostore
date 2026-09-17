"""Task 66. A banner below the admin header while SMS credit is low or out.

Requested by the user after seeing the bell alert in a live demo: the bell
is easy to miss. User-confirmed behaviour (2026-09-17):
- amber while credit is low, red once it has run out;
- the close button hides it for the rest of that login; it comes back at
  the next login while credit is still short;
- once credit is back above the warning level it disappears, and only
  comes back when credit falls below again;
- a "check now" button re-reads the balance (free, no SMS), so the admin
  doesn't wait up to an hour after topping up.
"""

from unittest.mock import patch

from django.urls import reverse

import pytest
from constance import config
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.notifications.sms import SmsSendError
from apps.notifications.sms_credit_status import record_sms_credit

BANNER = 'id="sms-credit-banner"'


@pytest.fixture(autouse=True)
def threshold():
    config.SMS_LOW_CREDIT_THRESHOLD = 200


def _page(client):
    return client.get(reverse("admin_portal:dashboard")).content.decode()


def _dismiss(client):
    return client.post(reverse("admin_portal:sms_credit_banner_dismiss"))


def _log_in_again(client):
    """Log out and back in as the same admin, with a verified 2FA session,
    as staff_client does."""
    from django.contrib.auth import get_user

    from django_otp import DEVICE_ID_SESSION_KEY

    user = get_user(client)
    client.logout()
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = TOTPDevice.objects.get(user=user).persistent_id
    session.save()


# --- Showing it -------------------------------------------------------------


@pytest.mark.django_db
def test_low_credit_shows_an_amber_banner_with_the_count(staff_client):
    record_sms_credit(150)

    html = _page(staff_client)

    assert BANNER in html
    assert 'data-kind="low"' in html
    assert "150" in html


@pytest.mark.django_db
def test_no_credit_shows_a_red_banner(staff_client):
    record_sms_credit(0)

    html = _page(staff_client)

    assert 'data-kind="out"' in html
    assert "run out" in html


@pytest.mark.django_db
def test_healthy_or_unknown_credit_shows_no_banner(staff_client):
    assert BANNER not in _page(staff_client)  # never checked yet

    record_sms_credit(1600)
    assert BANNER not in _page(staff_client)


@pytest.mark.django_db
def test_the_banner_sticks_below_the_header(staff_client):
    record_sms_credit(150)

    html = _page(staff_client)
    banner_at = html.index(BANNER)

    assert html.index("</header>") < banner_at  # below the header
    tag = html[html.rindex("<", 0, banner_at) : html.index(">", banner_at)]
    assert "sticky" in tag and "top-16" in tag  # stays put under the 4rem header


@pytest.mark.django_db
def test_the_banner_is_only_on_admin_pages(client, staff_client):
    record_sms_credit(0)

    assert BANNER not in client.get(reverse("catalog:home")).content.decode()


# --- Closing it -------------------------------------------------------------


@pytest.mark.django_db
def test_closing_hides_it_for_the_rest_of_the_login(staff_client):
    record_sms_credit(150)

    response = _dismiss(staff_client)

    assert response.status_code in (200, 204, 302)
    assert BANNER not in _page(staff_client)
    assert BANNER not in _page(staff_client)  # still hidden on the next page


@pytest.mark.django_db
def test_it_comes_back_at_the_next_login_while_credit_is_still_low(staff_client):
    record_sms_credit(150)
    _dismiss(staff_client)

    _log_in_again(staff_client)

    assert BANNER in _page(staff_client)


@pytest.mark.django_db
def test_running_out_after_closing_the_low_banner_shows_it_again(staff_client):
    """ "Low" and "run out" are different news."""
    record_sms_credit(150)
    _dismiss(staff_client)

    record_sms_credit(0)

    assert 'data-kind="out"' in _page(staff_client)


@pytest.mark.django_db
def test_a_new_shortage_in_the_same_login_shows_it_again(staff_client):
    record_sms_credit(150)
    _dismiss(staff_client)

    record_sms_credit(1600)  # topped up
    record_sms_credit(150)  # and later low again

    assert BANNER in _page(staff_client)


# --- Check now --------------------------------------------------------------


@pytest.mark.django_db
def test_check_now_after_a_top_up_hides_the_banner(staff_client):
    record_sms_credit(150)

    with patch(
        "apps.admin_portal.views.get_sms_credit_balance", return_value=1613
    ) as balance:
        response = staff_client.post(reverse("admin_portal:sms_credit_banner_check"))

    balance.assert_called_once()
    assert response.status_code == 200
    assert "1,613" in response.content.decode() or "1613" in response.content.decode()
    assert BANNER not in _page(staff_client)


@pytest.mark.django_db
def test_check_now_while_still_low_keeps_the_banner_with_the_new_count(staff_client):
    record_sms_credit(150)

    with patch("apps.admin_portal.views.get_sms_credit_balance", return_value=120):
        response = staff_client.post(reverse("admin_portal:sms_credit_banner_check"))

    assert 'data-kind="low"' in response.content.decode()
    assert "120" in response.content.decode()


@pytest.mark.django_db
def test_check_now_explains_when_mnotify_cannot_be_reached(staff_client):
    record_sms_credit(150)

    with patch(
        "apps.admin_portal.views.get_sms_credit_balance",
        side_effect=SmsSendError("down"),
    ):
        response = staff_client.post(reverse("admin_portal:sms_credit_banner_check"))

    assert response.status_code == 200
    assert "couldn&#x27;t reach mNotify" in response.content.decode()
    assert 'data-kind="low"' in response.content.decode()  # banner stays


# --- Who may use it ---------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name", ["sms_credit_banner_dismiss", "sms_credit_banner_check"]
)
def test_non_staff_are_refused(client, django_user_model, url_name):
    user = django_user_model.objects.create_user(
        username="nosy", password="x-Passw0rd!"
    )
    client.force_login(user)

    with patch("apps.admin_portal.views.get_sms_credit_balance") as balance:
        response = client.post(reverse(f"admin_portal:{url_name}"))

    assert response.status_code == 403
    balance.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name", ["sms_credit_banner_dismiss", "sms_credit_banner_check"]
)
def test_both_actions_are_post_only(staff_client, url_name):
    response = staff_client.get(reverse(f"admin_portal:{url_name}"))

    assert response.status_code == 405


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name", ["sms_credit_banner_dismiss", "sms_credit_banner_check"]
)
def test_both_actions_refuse_a_request_without_a_csrf_token(staff_client, url_name):
    """CodeRabbit (PR #96). The test client skips CSRF checks by default, so
    nothing else here would notice if the protection were removed."""
    staff_client.handler.enforce_csrf_checks = True

    with patch("apps.admin_portal.views.get_sms_credit_balance") as balance:
        response = staff_client.post(reverse(f"admin_portal:{url_name}"))

    assert response.status_code == 403
    balance.assert_not_called()
