from django.conf import settings

import requests
from constance import config

# Mirrors Django's django.core.mail.outbox pattern for the console/test email
# backend — lets tests assert on what was "sent" without hitting mNotify.
fake_outbox = []


def send_sms(phone_number: str, message: str, *, sms_type: str | None = None) -> None:
    if settings.MNOTIFY_API_KEY:
        _send_via_mnotify(phone_number, message, sms_type=sms_type)
    else:
        _send_via_fake_sender(phone_number, message)


def _send_via_fake_sender(phone_number: str, message: str) -> None:
    print(f"[fake SMS] to {phone_number}: {message}")
    fake_outbox.append({"phone_number": phone_number, "message": message})


def _send_via_mnotify(phone_number: str, message: str, *, sms_type: str | None) -> None:
    # Task 48d: SENDER_NAME defaults to blank -- "use the server's
    # configured default" (settings.MNOTIFY_SENDER_ID) -- with a live
    # admin override taking priority when set, mirroring
    # apps.notifications.email.get_sender_email()'s exact fallback shape.
    payload = {
        "recipient": [phone_number],
        "sender": config.SENDER_NAME or settings.MNOTIFY_SENDER_ID,
        "message": message,
        "is_schedule": False,
        "schedule_date": "",
    }
    # mNotify: don't send sms_type "otp" unless the message actually is one —
    # it changes how the message is billed/categorized on their side.
    if sms_type:
        payload["sms_type"] = sms_type

    response = requests.post(
        "https://api.mnotify.com/api/sms/quick",
        params={"key": settings.MNOTIFY_API_KEY},
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
