import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

import pytest
from constance import config

from apps.accounts.models import CustomerProfile

User = get_user_model()

RESET_LINK_PATTERN = re.compile(r"http\S+password/reset/key/\S+")


@pytest.mark.django_db
def test_customer_can_register_logout_and_login(client):
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Ama Mensah",
            "email": "ama@example.test",
            "phone_number": "+233241234567",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )

    # Task 56: signup used to redirect to Django's default LOGIN_REDIRECT_URL
    # (/accounts/profile/), which has never existed -- a real 404 for every new
    # customer. It now lands on the storefront home page.
    assert response.status_code == 302
    assert response.url == reverse("catalog:home")
    user = User.objects.get(email="ama@example.test")
    assert user.customer_profile.full_name == "Ama Mensah"
    assert str(user.customer_profile.phone_number) == "+233241234567"
    assert user.groups.filter(name="customer").exists()

    client.post(reverse("account_logout"))
    assert "_auth_user_id" not in client.session

    login_response = client.post(
        reverse("account_login"),
        {"login": "ama@example.test", "password": "S3cure-Passw0rd!"},
    )

    assert login_response.status_code == 302
    assert login_response.url == reverse("catalog:home")
    assert int(client.session["_auth_user_id"]) == user.id


@pytest.mark.django_db
def test_customer_login_form_keeps_the_next_page_and_returns_there(client):
    """Task 56: templates/account/login.html never rendered allauth's
    redirect field, so a ?next= target was silently dropped when the form
    posted and the customer landed on the default page instead of where they
    were going."""
    User.objects.create_user(
        username="esi", email="esi@example.test", password="Passw0rd-Esi!"
    )
    my_orders = reverse("orders:order_history")

    page = client.get(f"{reverse('account_login')}?next={my_orders}")
    assert (
        f'<input type="hidden" name="next" value="{my_orders}">'
        in page.content.decode()
    )

    response = client.post(
        reverse("account_login"),
        {
            "login": "esi@example.test",
            "password": "Passw0rd-Esi!",
            "next": my_orders,
        },
    )

    assert response.status_code == 302
    assert response.url == my_orders


@pytest.mark.django_db
def test_customer_signup_form_keeps_the_next_page(client):
    my_orders = reverse("orders:order_history")

    page = client.get(f"{reverse('account_signup')}?next={my_orders}")

    assert (
        f'<input type="hidden" name="next" value="{my_orders}">'
        in page.content.decode()
    )


@pytest.mark.django_db
def test_customer_login_ignores_an_offsite_next_and_lands_on_home(client):
    """Now that the form carries `next`, it must not become an open redirect:
    allauth only follows it when adapter.is_safe_url() allows it."""
    User.objects.create_user(
        username="yaw", email="yaw@example.test", password="Passw0rd-Yaw!"
    )

    response = client.post(
        reverse("account_login"),
        {
            "login": "yaw@example.test",
            "password": "Passw0rd-Yaw!",
            "next": "https://evil.example/phish",
        },
    )

    assert response.status_code == 302
    assert response.url == reverse("catalog:home")


@pytest.mark.django_db
def test_registration_enforces_the_live_min_password_length(client):
    """Task 30a: MIN_PASSWORD_LENGTH was previously fully decorative --
    confirms an admin-editable value is actually enforced end-to-end
    through the real registration endpoint, not just at the validator
    unit level."""
    config.MIN_PASSWORD_LENGTH = 12

    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Kojo Boateng",
            "email": "kojo@example.test",
            "phone_number": "+233241234568",
            "password1": "Sh0rt-Px!",
            "password2": "Sh0rt-Px!",
            "terms_accepted": "on",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(email="kojo@example.test").exists()


@pytest.mark.django_db
def test_registration_enforces_password_complexity_when_enabled(client):
    """Task 30a: PASSWORD_COMPLEXITY_ENABLED had no enforcement mechanism
    at all before this -- confirms a password with no uppercase letter
    and no digit is rejected end-to-end when the flag is on."""
    config.PASSWORD_COMPLEXITY_ENABLED = True

    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Adjoa Sarpong",
            "email": "adjoa@example.test",
            "phone_number": "+233241234569",
            "password1": "all-lowercase-no-digits",
            "password2": "all-lowercase-no-digits",
            "terms_accepted": "on",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(email="adjoa@example.test").exists()


@pytest.mark.django_db
def test_customer_can_register_with_a_local_format_phone_number(client):
    """Ghana numbers typed without +233 (e.g. "0545488681", as a real user
    would type it) must still validate — PHONENUMBER_DEFAULT_REGION makes
    this work. Regression test for a bug caught during manual testing."""
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Kojo Mensah",
            "email": "kojo2@example.test",
            "phone_number": "0545488681",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
            "terms_accepted": "on",
        },
    )

    assert response.status_code == 302
    user = User.objects.get(email="kojo2@example.test")
    assert str(user.customer_profile.phone_number) == "+233545488681"


@pytest.mark.django_db
def test_customer_cannot_register_without_accepting_terms(client):
    response = client.post(
        reverse("account_signup"),
        {
            "full_name": "Kojo Antwi",
            "email": "kojo@example.test",
            "phone_number": "+233241234568",
            "password1": "S3cure-Passw0rd!",
            "password2": "S3cure-Passw0rd!",
        },
    )

    assert response.status_code == 200
    assert not User.objects.filter(email="kojo@example.test").exists()
    assert "terms_accepted" in response.context["form"].errors


@pytest.mark.django_db
def test_customer_password_reset_completes_via_email_link(client):
    user = User.objects.create_user(
        username="kwame", email="kwame@example.test", password="OldPassw0rd!"
    )
    CustomerProfile.objects.create(
        user=user, full_name="Kwame Boateng", phone_number="+233551234567"
    )

    client.post(reverse("account_reset_password"), {"email": "kwame@example.test"})

    assert len(mail.outbox) == 1
    match = RESET_LINK_PATTERN.search(mail.outbox[0].body)
    assert match, "password reset email did not contain a reset link"
    reset_url = match.group(0)

    set_password_response = client.get(reset_url, follow=True)
    assert set_password_response.status_code == 200

    final_url = set_password_response.redirect_chain[-1][0]
    client.post(
        final_url,
        {"password1": "NewPassw0rd!", "password2": "NewPassw0rd!"},
    )

    user.refresh_from_db()
    assert user.check_password("NewPassw0rd!")


@pytest.mark.django_db
def test_customer_password_reset_email_links_to_customer_confirm_page(client):
    """Task 55b: the admin-branded reset link now comes from the project-wide
    ACCOUNT_ADAPTER's get_reset_password_from_key_url hook, which every
    allauth password reset goes through -- not just the admin one. A
    customer requesting a reset from the storefront must still get allauth's
    own customer-facing confirm page, never the admin-branded one."""
    from urllib.parse import urlparse

    from django.urls import resolve

    User.objects.create_user(
        username="kwame", email="kwame@example.test", password="OldPassw0rd!"
    )

    client.post(reverse("account_reset_password"), {"email": "kwame@example.test"})

    match = RESET_LINK_PATTERN.search(mail.outbox[0].body)
    reset_path = urlparse(match.group(0)).path

    assert resolve(reset_path).url_name == "account_reset_password_from_key"
