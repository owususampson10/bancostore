"""
ASGI config for bancostore project.

Exposes the ASGI callable as a module-level variable named ``application``,
routing HTTP through Django and WebSockets through Channels/Redis.
"""

import os

from django.conf import settings
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from django.core.asgi import get_asgi_application

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bancostore.settings")

django_asgi_app = get_asgi_application()

# Unlike `runserver`, a plain ASGI server (daphne) does not auto-serve
# STATIC_URL in DEBUG mode -- that auto-serving is runserver's own
# behavior, not a generic property of DEBUG. Without this, every static
# asset 404s/503s under daphne specifically (found via real-browser
# testing while verifying Task 20d -- no pytest run can ever catch this,
# same class of gap as the documented MEDIA_URL issue above, since
# Django's test runner always forces DEBUG=False). Production doesn't
# need this either way -- Nginx serves static files directly there.
if settings.DEBUG:
    django_asgi_app = ASGIStaticFilesHandler(django_asgi_app)


def _websocket_urlpatterns():
    """Every consumer route in the project, in one list.

    Imported inside a function, not at module level: each routing module
    imports consumers.py, which imports models, so importing any of them
    before get_asgi_application() has called django.setup() raises
    AppRegistryNotReady. A module-level import here would additionally
    trip ruff's E402 (module import not at top of file) and need a
    suppression comment -- Task 61c removed the two that used to sit
    here by moving the imports into this function, where E402 does not
    apply at all. Called once below, after setup() has run.
    """
    from apps.admin_portal.routing import websocket_urlpatterns as admin_patterns
    from apps.distributors.routing import websocket_urlpatterns as distributor_patterns

    return distributor_patterns + admin_patterns


application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # AllowedHostsOriginValidator rejects a WebSocket handshake whose
        # Origin header isn't in settings.ALLOWED_HOSTS -- without it,
        # session-cookie auth alone is vulnerable to cross-site WebSocket
        # hijacking (a fresh-context review flagged this for Task 20d,
        # since this channel carries real wallet-balance data).
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(_websocket_urlpatterns()))
        ),
    }
)
