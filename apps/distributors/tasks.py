from datetime import timedelta

from django.utils import timezone

from celery import shared_task

from .models import PendingRegistration

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
