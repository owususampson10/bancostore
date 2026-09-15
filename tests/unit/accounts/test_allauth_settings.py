from allauth.account import app_settings
from allauth.account.checks import settings_check


def test_allauth_settings_raise_no_system_check_messages():
    """Task 55c: django-allauth 65.x warns on ACCOUNT_AUTHENTICATION_METHOD
    (-> ACCOUNT_LOGIN_METHODS, 65.4) and ACCOUNT_EMAIL_REQUIRED /
    ACCOUNT_USERNAME_REQUIRED (-> ACCOUNT_SIGNUP_FIELDS, 65.5). Those warnings
    carry no check id, so this calls allauth's own settings_check directly.
    An empty result also covers account.W001, allauth's cross-check that a
    login method is a required signup field."""
    assert settings_check(None) == []


def test_customers_log_in_by_email_only():
    """Pins the effective value the old ACCOUNT_AUTHENTICATION_METHOD="email"
    resolved to, so the settings migration cannot change login behavior."""
    assert app_settings.LOGIN_METHODS == frozenset({app_settings.LoginMethod.EMAIL})


def test_signup_requires_email_and_password_twice_with_no_username():
    """Pins the effective value the old ACCOUNT_EMAIL_REQUIRED=True /
    ACCOUNT_USERNAME_REQUIRED=False (plus allauth's default of entering the
    password twice) resolved to. full_name, phone_number and terms_accepted
    are CustomerSignupForm's own fields, not allauth signup fields."""
    assert app_settings.SIGNUP_FIELDS == {
        "email": {"required": True},
        "password1": {"required": True},
        "password2": {"required": True},
    }
