from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/distributors/wallet/$", consumers.WalletBalanceConsumer.as_asgi()),
    re_path(
        r"^ws/distributors/notifications/$", consumers.NotificationConsumer.as_asgi()
    ),
]
