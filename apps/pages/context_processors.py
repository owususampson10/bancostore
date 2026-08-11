import logging

from django.core.cache import cache

from .models import SOCIAL_MEDIA_LINKS_CACHE_KEY, SocialMediaLink

logger = logging.getLogger(__name__)

# CodeRabbit finding: primarily invalidated by SocialMediaLink's own
# post_save/post_delete signal (models.py), not by expiry -- but a
# non-expiring entry has no self-healing floor if a future bulk write
# (e.g. a management command doing a raw .update()/.bulk_create() that
# bypasses the model's save()/delete(), and therefore the signal) ever
# left it stale. A day is long enough that this timeout is never the
# thing normal admin edits rely on, short enough to be a real safety net.
_CACHE_TIMEOUT_SECONDS = 60 * 60 * 24


def social_media_links(request):
    """Runs on every request across the whole site (base_store.html's
    footer is shared by every storefront page) -- cached and invalidated
    by SocialMediaLink's own post_save/post_delete signal (models.py), the
    same "no per-request DB hit for something admin rarely changes"
    reasoning constance's own Redis cache already applies to business-rule
    settings (doubt-driven-development finding).

    CodeRabbit finding: django_redis raises on a real connection failure
    (no IGNORE_EXCEPTIONS set in CACHES, bancostore/settings.py) rather
    than degrading gracefully -- an uncaught exception here would 500 the
    *entire* storefront on every single page load, not just this footer
    section, since this context processor runs before every template
    render site-wide. Falls back to a real DB read (the actually-correct
    data, not stale) on any cache failure, on both the read and the write
    side, logged so a real Redis outage is still visible in monitoring."""
    try:
        links = cache.get(SOCIAL_MEDIA_LINKS_CACHE_KEY)
    except Exception:
        logger.exception(
            "social_media_links: cache read failed, falling back to the database"
        )
        links = None

    if links is None:
        links = list(SocialMediaLink.objects.all())
        try:
            cache.set(
                SOCIAL_MEDIA_LINKS_CACHE_KEY, links, timeout=_CACHE_TIMEOUT_SECONDS
            )
        except Exception:
            logger.exception("social_media_links: cache write failed")

    return {"social_media_links": links}
