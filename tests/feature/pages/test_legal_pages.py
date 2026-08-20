from decimal import Decimal

from django.contrib.humanize.templatetags.humanize import intcomma
from django.urls import reverse

import pytest
from constance import config

"""Task 34: 7 legal/policy pages, grouped under a new "Policies" footer
column in base_store.html. One 200-status test per page plus a footer
link-presence check, matching test_about.py/test_navigation_links.py's own
established conventions."""

LEGAL_URL_NAMES = [
    "pages:terms_of_use",
    "pages:privacy_policy",
    "pages:cookie_policy",
    "pages:disclaimer",
    "pages:earnings_disclosure",
    "pages:ai_disclaimer",
    "pages:returns_refunds_shipping",
    "pages:getting_started_guide",
]


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", LEGAL_URL_NAMES)
def test_legal_page_renders(client, url_name):
    response = client.get(reverse(url_name))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Policies" in content
    assert reverse("pages:contact") in content


@pytest.mark.django_db
def test_footer_links_to_every_policy_page(client):
    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "Policies" in content
    for url_name in LEGAL_URL_NAMES:
        assert f'href="{reverse(url_name)}"' in content


@pytest.mark.django_db
def test_getting_started_guide_shows_real_business_rule_values(client):
    """Every number on this guide (registration fee, starter pack prices,
    commission rates, withdrawal bounds) is admin-configurable via
    constance -- never hardcoded -- matching returns_refunds_shipping's
    own established pattern above. A stale hardcoded number here would
    silently mislead a new distributor about what they'll actually pay
    or earn."""
    response = client.get(reverse("pages:getting_started_guide"))

    content = response.content.decode()
    # The template renders these through |intcomma (matching
    # returns_refunds_shipping's own convention), so expected values must
    # be comma-formatted too, not just str(int(...)).
    assert intcomma(int(config.REGISTRATION_FEE)) in content
    assert intcomma(int(config.STARTER_PACK_A_PRICE)) in content
    assert intcomma(int(config.STARTER_PACK_B_PRICE)) in content
    assert str(config.DIRECT_REFERRAL_BONUS_RATE) in content
    assert str(config.BINARY_BONUS_RATE) in content
    assert str(config.MATCHING_BONUS_RATE) in content
    assert intcomma(int(config.MIN_WITHDRAWAL_AMOUNT)) in content
    assert intcomma(int(config.MAX_WITHDRAWAL_AMOUNT)) in content
    assert str(config.WITHHOLDING_TAX_RATE) in content


@pytest.mark.django_db
def test_getting_started_guide_reflects_a_live_admin_change(client):
    """Regression guard against the exact bug class this project has hit
    before (see the docstring on terms_of_use's cooling_off_period_days):
    a figure hardcoded as plain text instead of reading the live setting.
    Changing a rate must actually change what renders. Constance state is
    process-wide, not per-test-transaction-isolated, so the original value
    is restored afterward -- matching tests/unit/commissions/
    test_direct_referral.py's own established pattern for this exact
    setting."""
    original_rate = config.DIRECT_REFERRAL_BONUS_RATE
    config.DIRECT_REFERRAL_BONUS_RATE = Decimal("42")
    try:
        response = client.get(reverse("pages:getting_started_guide"))
        assert "42" in response.content.decode()
    finally:
        config.DIRECT_REFERRAL_BONUS_RATE = original_rate


@pytest.mark.django_db
def test_getting_started_guide_matching_bonus_silver_depth_is_read_live(client):
    """Regression test for a real bug caught in review: the template
    originally hardcoded "unlimited depth" for Silver rank as static text
    instead of reading MATCHING_BONUS_DEPTH_SILVER, even though the view
    already passed it into context -- an admin setting a real positive
    depth would have silently gone unreflected. 0 means unlimited, per
    config.py's own documented convention."""
    original_depth = config.MATCHING_BONUS_DEPTH_SILVER
    try:
        config.MATCHING_BONUS_DEPTH_SILVER = 0
        response = client.get(reverse("pages:getting_started_guide"))
        assert "unlimited depth" in response.content.decode()

        config.MATCHING_BONUS_DEPTH_SILVER = 6
        response = client.get(reverse("pages:getting_started_guide"))
        content = response.content.decode()
        assert "6 levels deep" in content
        assert "unlimited depth" not in content
    finally:
        config.MATCHING_BONUS_DEPTH_SILVER = original_depth


@pytest.mark.django_db
def test_returns_refunds_shipping_shows_real_delivery_fees(client):
    """Grounded in the real, live constance settings -- never a hardcoded
    guess, matching this project's own "business rules live in settings,
    not code" rule (apps/orders/views.py's checkout view reads the same
    four DELIVERY_SETTINGS keys)."""
    response = client.get(reverse("pages:returns_refunds_shipping"))

    content = response.content.decode()
    assert str(int(config.DELIVERY_FEE_KUMASI)) in content
    assert str(int(config.DELIVERY_FEE_ACCRA)) in content
    assert str(int(config.DELIVERY_FEE_OTHER_REGIONS)) in content
    assert str(config.PENDING_ORDER_AUTO_CANCEL_HOURS) in content


@pytest.mark.django_db
def test_returns_refunds_shipping_shows_admin_custom_policy_text_when_set(client):
    config.REFUND_RETURN_POLICY_TEXT = "A custom admin-authored refund clause."

    response = client.get(reverse("pages:returns_refunds_shipping"))

    assert response.status_code == 200
    assert "A custom admin-authored refund clause." in response.content.decode()


@pytest.mark.django_db
def test_returns_refunds_shipping_hides_custom_policy_block_when_unset(client):
    config.REFUND_RETURN_POLICY_TEXT = "   "

    response = client.get(reverse("pages:returns_refunds_shipping"))

    assert response.status_code == 200
    assert "Additional Policy Details" not in response.content.decode()


@pytest.mark.django_db
def test_returns_refunds_shipping_escapes_admin_policy_text(client):
    """Code-review finding: the security review confirmed `linebreaks`
    auto-escapes, but nothing pinned that down as a regression test."""
    config.REFUND_RETURN_POLICY_TEXT = "<script>alert(1)</script>"

    response = client.get(reverse("pages:returns_refunds_shipping"))

    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content


@pytest.mark.django_db
def test_terms_of_use_shows_admin_custom_text_when_set(client):
    config.TERMS_AND_CONDITIONS_TEXT = "A custom admin-authored clause."

    response = client.get(reverse("pages:terms_of_use"))

    assert "A custom admin-authored clause." in response.content.decode()


@pytest.mark.django_db
def test_terms_of_use_hides_custom_text_block_when_unset(client):
    config.TERMS_AND_CONDITIONS_TEXT = "   "

    response = client.get(reverse("pages:terms_of_use"))

    assert "Additional Terms" not in response.content.decode()


@pytest.mark.django_db
def test_terms_of_use_escapes_admin_text(client):
    config.TERMS_AND_CONDITIONS_TEXT = "<script>alert(1)</script>"

    response = client.get(reverse("pages:terms_of_use"))

    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content


@pytest.mark.django_db
def test_privacy_policy_shows_admin_custom_text_when_set(client):
    config.PRIVACY_POLICY_TEXT = "A custom admin-authored clause."

    response = client.get(reverse("pages:privacy_policy"))

    assert "A custom admin-authored clause." in response.content.decode()


@pytest.mark.django_db
def test_privacy_policy_hides_custom_text_block_when_unset(client):
    config.PRIVACY_POLICY_TEXT = "   "

    response = client.get(reverse("pages:privacy_policy"))

    assert "Additional Privacy Details" not in response.content.decode()


@pytest.mark.django_db
def test_privacy_policy_escapes_admin_text(client):
    """CodeRabbit finding: TERMS_AND_CONDITIONS_TEXT and
    REFUND_RETURN_POLICY_TEXT each had this regression test already --
    PRIVACY_POLICY_TEXT didn't."""
    config.PRIVACY_POLICY_TEXT = "<script>alert(1)</script>"

    response = client.get(reverse("pages:privacy_policy"))

    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content


@pytest.mark.django_db
def test_terms_of_use_shows_the_real_cooling_off_period(client):
    """CodeRabbit finding: this figure was hardcoded as plain text
    instead of reading the live COOLING_OFF_PERIOD_DAYS setting."""
    config.COOLING_OFF_PERIOD_DAYS = 10

    response = client.get(reverse("pages:terms_of_use"))

    content = response.content.decode()
    assert "10 days" in content
    assert "7 days" not in content


@pytest.mark.django_db
def test_signup_consent_links_point_to_real_legal_pages(client):
    response = client.get(reverse("account_signup"))

    content = response.content.decode()
    assert f'href="{reverse("pages:terms_of_use")}"' in content
    assert f'href="{reverse("pages:privacy_policy")}"' in content


@pytest.mark.django_db
def test_distributor_register_consent_link_points_to_a_real_legal_page(client):
    response = client.get(reverse("distributors:register"))

    content = response.content.decode()
    assert f'href="{reverse("pages:terms_of_use")}"' in content


@pytest.mark.django_db
def test_ai_disclaimer_covers_ai_generated_imagery(client):
    response = client.get(reverse("pages:ai_disclaimer"))

    content = response.content.decode().lower()
    assert "ai-generated" in content
    assert "product" in content


@pytest.mark.django_db
def test_earnings_disclosure_states_no_guaranteed_income(client):
    response = client.get(reverse("pages:earnings_disclosure"))

    content = response.content.decode().lower()
    assert (
        "no income is ever guaranteed" in content or "no guaranteed income" in content
    )
