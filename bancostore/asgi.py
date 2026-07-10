"""
ASGI config for bancostore project.

Exposes the ASGI callable as a module-level variable named ``application``,
routing HTTP through Django and WebSockets through Channels/Redis.
"""

import os

from django.core.asgi import get_asgi_application

from channels.routing import ProtocolTypeRouter, URLRouter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bancostore.settings")

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter([]),
    }
)
