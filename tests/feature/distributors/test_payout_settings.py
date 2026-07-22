import json
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

import pytest

from apps.distributors.models import Distributor

User = get_user_model()


def _make_distributor(phone="+233247000001"):
    """Mirrors apps/distributors/services.py::consume_paid_registration's
    real account-creation shape -- see test_earnings_history.py for the
    same convention."""
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    return Distributor.objects.create(user=user, phone_number=phone)


def _login(client, distributor, password="Passw0rd!"):
    client.login(phone_number=str(distributor.phone_number), password=password)


@pytest.mark.django_db
def test_payout_settings_requires_login(client):
    response = client.get(reverse("distributors:payout_settings"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("distributors:login"))


@pytest.mark.django_db
def test_authenticated_non_distributor_gets_403_not_a_crash(client):
    user = User.objects.create_user(username="customer-1", password="Passw0rd!")
    client.force_login(user)

    response = client.get(reverse("distributors:payout_settings"))

    assert response.status_code == 403


@pytest.mark.django_db
def test_shows_empty_state_when_nothing_set_yet(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.get(reverse("distributors:payout_settings"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "No payout destination set" in body


@pytest.mark.django_db
def test_can_set_payout_destination(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "+233247111222",
            "mobile_money_network": "mtn",
        },
        follow=True,
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.mobile_money_number == "+233247111222"
    assert distributor.mobile_money_network == "mtn"
    assert "Payout details saved" in response.content.decode()


@pytest.mark.django_db
def test_shows_currently_saved_destination(client):
    distributor = _make_distributor()
    distributor.mobile_money_number = "+233247111222"
    distributor.mobile_money_network = "mtn"
    distributor.save()
    _login(client, distributor)

    response = client.get(reverse("distributors:payout_settings"))

    body = response.content.decode()
    assert "233247111222" in body
    assert "MTN MoMo" in body


@pytest.mark.django_db
def test_can_update_an_existing_destination(client):
    distributor = _make_distributor()
    distributor.mobile_money_number = "+233247111222"
    distributor.mobile_money_network = "mtn"
    distributor.save()
    _login(client, distributor)

    client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "+233247999888",
            "mobile_money_network": "telecel",
        },
    )

    distributor.refresh_from_db()
    assert distributor.mobile_money_number == "+233247999888"
    assert distributor.mobile_money_network == "telecel"


@pytest.mark.django_db
def test_rejects_invalid_network_choice(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "+233247111222",
            "mobile_money_network": "not-a-real-network",
        },
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.mobile_money_network == ""
    assert "valid choice" in response.content.decode().lower()


@pytest.mark.django_db
def test_network_field_injection_is_never_reflected_unescaped(client):
    """Regression test for a real XSS bug caught by code-review-and-quality
    (2026-07-22): the first version of this page interpolated the raw
    mobile_money_network POST value directly into an Alpine x-data JS
    expression. HTML-attribute escaping doesn't protect that context --
    the browser HTML-decodes the attribute before Alpine evaluates it as
    JS, so a crafted value could break out of the JS string literal and
    execute script in the distributor's own session. Fixed by constraining
    the value to the known-good network choices server-side (an invalid
    value becomes "", never reflected at all) and passing data to Alpine
    via json_script rather than raw interpolation. This proves the fix:
    an injection payload targeting exactly that JS-string-breakout shape
    never appears verbatim in the response, however it's assembled."""
    distributor = _make_distributor()
    _login(client, distributor)
    payload = "x', (function(){window.__pwned=true})(), y:'z"

    response = client.post(
        reverse("distributors:payout_settings"),
        {"mobile_money_number": "+233247111222", "mobile_money_network": payload},
    )

    assert response.status_code == 200
    body = response.content.decode()
    # The raw payload is never reflected unescaped -- Django's own form
    # error message does legitimately echo the submitted value back as
    # plain, properly-escaped text ("...is not one of the available
    # choices"), which is safe and expected; what must never happen is
    # the payload reaching the json_script block Alpine actually parses
    # as data, since that's the channel the original bug exploited.
    assert payload not in body
    match = re.search(
        r'<script id="momo-network-selected"[^>]*>(.*?)</script>', body, re.DOTALL
    )
    assert match is not None
    assert json.loads(match.group(1)) == ""
    distributor.refresh_from_db()
    assert distributor.mobile_money_network == ""


@pytest.mark.django_db
def test_rejects_malformed_mobile_money_number(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "not-a-phone-number",
            "mobile_money_network": "mtn",
        },
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.mobile_money_number == ""


@pytest.mark.django_db
def test_rejects_number_without_network(client):
    """Both fields are required together on the form -- a distributor
    can't submit a payout number with no network selected."""
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:payout_settings"),
        {"mobile_money_number": "+233247111222", "mobile_money_network": ""},
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.mobile_money_number == ""


@pytest.mark.django_db
def test_saving_payout_details_does_not_clobber_a_concurrent_unrelated_field_change(
    client, monkeypatch
):
    """CodeRabbit review (2026-07-22): the view loads `distributor` once at
    request start, then saves the whole row at the end -- a full-row save()
    would silently overwrite any other field changed by a concurrent
    process (e.g. an admin KYC action) between that load and this save.

    A plain sequential test can't reproduce this race directly (nothing
    can run "between" two lines of a single-threaded view call without
    intervening at that exact point) -- so this wraps Distributor.save()
    itself to inject the concurrent write at precisely the moment a real
    race would land: after the view's own in-memory `distributor` object
    was loaded, but immediately before it persists."""
    distributor = _make_distributor()
    _login(client, distributor)
    assert distributor.kyc_status == Distributor.KycStatus.PENDING

    original_save = Distributor.save

    def save_after_concurrent_kyc_approval(self, *args, **kwargs):
        Distributor.objects.filter(pk=self.pk).update(
            kyc_status=Distributor.KycStatus.APPROVED
        )
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Distributor, "save", save_after_concurrent_kyc_approval)

    client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "+233247111222",
            "mobile_money_network": "mtn",
        },
    )

    distributor.refresh_from_db()
    assert distributor.mobile_money_number == "+233247111222"
    assert distributor.kyc_status == Distributor.KycStatus.APPROVED


@pytest.mark.django_db
def test_rejects_network_without_number(client):
    distributor = _make_distributor()
    _login(client, distributor)

    response = client.post(
        reverse("distributors:payout_settings"),
        {"mobile_money_number": "", "mobile_money_network": "mtn"},
    )

    assert response.status_code == 200
    distributor.refresh_from_db()
    assert distributor.mobile_money_network == ""


@pytest.mark.django_db
def test_never_shows_or_updates_another_distributors_payout_details(client):
    own = _make_distributor(phone="+233247000001")
    other = _make_distributor(phone="+233247000002")
    other.mobile_money_number = "+233247999888"
    other.mobile_money_network = "mtn"
    other.save()
    _login(client, own)

    response = client.get(reverse("distributors:payout_settings"))

    assert "233247999888" not in response.content.decode()

    client.post(
        reverse("distributors:payout_settings"),
        {
            "mobile_money_number": "+233247555666",
            "mobile_money_network": "telecel",
        },
    )
    other.refresh_from_db()
    assert other.mobile_money_number == "+233247999888"
    assert other.mobile_money_network == "mtn"
