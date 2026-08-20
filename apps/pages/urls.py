from django.urls import path

from . import views

app_name = "pages"

urlpatterns = [
    path("about/", views.about, name="about"),
    path("contact/", views.contact, name="contact"),
    path("terms-of-use/", views.terms_of_use, name="terms_of_use"),
    path("privacy-policy/", views.privacy_policy, name="privacy_policy"),
    path("cookie-policy/", views.cookie_policy, name="cookie_policy"),
    path("disclaimer/", views.disclaimer, name="disclaimer"),
    path(
        "earnings-disclosure/",
        views.earnings_disclosure,
        name="earnings_disclosure",
    ),
    path("ai-disclaimer/", views.ai_disclaimer, name="ai_disclaimer"),
    path(
        "returns-refunds-shipping/",
        views.returns_refunds_shipping,
        name="returns_refunds_shipping",
    ),
    path(
        "getting-started-guide/",
        views.getting_started_guide,
        name="getting_started_guide",
    ),
]
