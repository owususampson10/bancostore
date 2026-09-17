from django.conf import settings

import requests
from constance import config

MNOTIFY_SEND_URL = "https://api.mnotify.com/api/sms/quick"
# Shape verified live against production on 2026-09-17:
# {"status":"success","wallet":"59.3","balance":0,"bonus":0}
MNOTIFY_BALANCE_URL = "https://api.mnotify.com/api/balance/sms"

# Mirrors Django's django.core.mail.outbox pattern for the console/test email
# backend — lets tests assert on what was "sent" without hitting mNotify.
fake_outbox = []


class SmsSendError(Exception):
    """An SMS could not be sent, or mNotify could not be asked.

    Task 63a. Raised in place of requests' own exceptions, deliberately
    WITHOUT chaining them (`from None`): mNotify takes the API key in the
    URL, and requests puts the full URL into its error messages. Every send
    site logs failures with logger.exception, which prints the whole
    exception chain -- so production's error logs held the live key on every
    failed send until this existed."""


class SmsOutOfCredit(SmsSendError):
    """mNotify refused the send because the account has no SMS credit
    (HTTP 402 Payment Required, seen live on 2026-09-17)."""


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

    try:
        response = requests.post(
            MNOTIFY_SEND_URL,
            params={"key": settings.MNOTIFY_API_KEY},
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise SmsSendError(
            f"could not reach mNotify to send an SMS ({type(exc).__name__})"
        ) from None

    if response.status_code == 402:
        raise SmsOutOfCredit("mNotify refused the SMS: the account is out of credit")
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise SmsSendError(
            f"mNotify refused the SMS with HTTP {response.status_code}"
        ) from None


def get_sms_credit_balance() -> int | None:
    """SMS credits left on the mNotify account, bonus credits included.

    None when no API key is configured (local dev and tests use the fake
    sender, which has no balance). Raises SmsSendError, never a requests
    exception, for the same key-in-the-URL reason as above.

    `wallet` is money (GHS) not yet converted into credits, so it is not
    counted: a message can't be sent with it until it is converted.
    """
    if not settings.MNOTIFY_API_KEY:
        return None
    try:
        response = requests.get(
            MNOTIFY_BALANCE_URL,
            params={"key": settings.MNOTIFY_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SmsSendError(
            f"could not read the mNotify credit balance ({type(exc).__name__})"
        ) from None

    if not isinstance(data, dict):
        data = {}
    balance = data.get("balance")
    bonus = data.get("bonus") or 0
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        raise SmsSendError("mNotify's balance response had no credit balance")
    if isinstance(bonus, bool) or not isinstance(bonus, (int, float)):
        bonus = 0
    return int(balance) + int(bonus)
