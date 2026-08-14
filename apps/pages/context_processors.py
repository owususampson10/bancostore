import logging

from constance import config
from django.core.cache import cache
from django.templatetags.static import static

from bancostore.json_ld import dumps_for_script_tag

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


def _get_cached_social_media_links():
    """Shared by social_media_links (footer) and organization_json_ld
    (Task 37c) so both read the same cached list instead of each running
    its own uncached query on every request -- see social_media_links'
    own docstring for the caching/fallback reasoning this mirrors."""
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

    return links


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
    return {"social_media_links": _get_cached_social_media_links()}


def organization_json_ld(request):
    """Task 37c: sitewide Organization/WebSite JSON-LD (base_store.html).
    Serialized here, not built with template tags, so a social link's name
    or URL can never produce broken/unescaped JSON -- dumps_for_script_tag
    (bancostore/json_ld.py) is the single source of truth for correct
    escaping, both for valid JSON syntax and for safety inside a <script>
    element (a literal "</script>" in an admin-entered social link name
    could otherwise break out of the tag -- CodeRabbit finding on PR #70,
    fixed here too even though it was only flagged on the Product JSON-LD
    twin of this function), matching this codebase's established
    "constrain server-side, never raw interpolation" rule (see Task 17's
    Alpine x-data XSS fix for the same reasoning applied to a different
    injection context)."""
    base_url = f"{request.scheme}://{request.get_host()}"
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Organization",
                "name": "Bancostore",
                "url": f"{base_url}/",
                "logo": base_url
                + static("bancostore-brand/logo/bancostore-logo-orange.svg"),
                "sameAs": [link.url for link in _get_cached_social_media_links()],
            },
            {
                "@type": "WebSite",
                "name": "Bancostore",
                "url": f"{base_url}/",
                "potentialAction": {
                    "@type": "SearchAction",
                    "target": {
                        "@type": "EntryPoint",
                        "urlTemplate": f"{base_url}/shop/?q={{search_term_string}}",
                    },
                    "query-input": "required name=search_term_string",
                },
            },
        ],
    }
    return {"organization_json_ld": dumps_for_script_tag(data)}


def free_delivery_threshold(request):
    """base_store.html's site-wide announcement bar (the header banner
    shown on every storefront page) advertised a hardcoded "GHS 1000"
    free-shipping threshold that never matched the real, admin-editable
    FREE_DELIVERY_THRESHOLD constance setting (seeded at GHS 500) --
    apps.orders.views/apps.pages.views already pass the live value to
    checkout.html/returns-refunds-shipping.html individually, but the
    banner lives in the shared base template so it needs a global
    context processor instead, same reasoning as google_login_flags.
    Constance's own config object is already Redis-cached, so this reads
    it directly rather than duplicating a cache layer.
    """
    try:
        threshold = config.FREE_DELIVERY_THRESHOLD
    except Exception:
        logger.warning(
            "free_delivery_threshold: could not read FREE_DELIVERY_THRESHOLD "
            "from constance, defaulting to the setting's own seeded default"
        )
        threshold = 500
    return {"free_delivery_threshold": threshold}
