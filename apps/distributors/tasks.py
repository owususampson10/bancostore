from datetime import timedelta

from django.utils import timezone

from celery import shared_task

from .models import PendingRegistration
from .services import consume_didit_result

PENDING_REGISTRATION_TTL = timedelta(hours=1)


@shared_task
def cleanup_expired_pending_registrations():
    """Deletes unconsumed PendingRegistration rows older than the TTL
    (confirmed with the user 2026-07-13) so abandoned registrations don't
    hold onboarding PII indefinitely."""
    cutoff = timezone.now() - PENDING_REGISTRATION_TTL
    PendingRegistration.objects.filter(
        consumed_at__isnull=True, created_at__lt=cutoff
    ).delete()


@shared_task
def consume_didit_result_task(session_id: str) -> None:
    """Task 11 (performance-optimization retrospective, 2026-07-14): Didit's
    own webhook docs (https://docs.didit.me/integration/webhooks) specify a
    5-second response timeout, 2 retries, then the delivery is dropped --
    and explicitly say to "return 2xx as soon as you have queued the work,
    do heavy processing asynchronously." consume_didit_result does a Didit
    API call plus up to 3 synchronous image downloads (10s timeout each),
    easily exceeding 5 seconds under any real network latency. This task
    just wraps that same function so apps/distributors/views.py::
    didit_webhook can enqueue it and return 200 immediately, instead of
    doing the work inline. The callback-redirect view (kyc_verification_
    callback) deliberately still calls consume_didit_result synchronously
    -- it's a browser redirect waiting on a "Confirming..." interstitial,
    not a third-party webhook with an enforced timeout."""
    consume_didit_result(session_id)
