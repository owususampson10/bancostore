"""
URL configuration for bancostore project.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from two_factor.urls import urlpatterns as two_factor_urls

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include(two_factor_urls)),
    path("silk/", include("silk.urls", namespace="silk")),
]

if settings.DEBUG:
    urlpatterns += [
        path("__debug__/", include("debug_toolbar.urls")),
    ]
