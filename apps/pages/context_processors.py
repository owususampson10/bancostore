from django.core.cache import cache

from .models import SOCIAL_MEDIA_LINKS_CACHE_KEY, SocialMediaLink


def social_media_links(request):
    """Runs on every request across the whole site (base_store.html's
    footer is shared by every storefront page) -- cached indefinitely and
    invalidated by SocialMediaLink's own post_save/post_delete signal
    (models.py), the same "no per-request DB hit for something admin
    rarely changes" reasoning constance's own Redis cache already applies
    to business-rule settings (doubt-driven-development finding)."""
    links = cache.get(SOCIAL_MEDIA_LINKS_CACHE_KEY)
    if links is None:
        links = list(SocialMediaLink.objects.all())
        cache.set(SOCIAL_MEDIA_LINKS_CACHE_KEY, links, timeout=None)
    return {"social_media_links": links}
