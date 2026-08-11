from django.core.cache import cache
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .social_icons import SOCIAL_ICON_CHOICES, get_icon

# Doubt-driven-development finding: a plain ^...$ pattern still admits a
# single trailing "\n" (Python re's $ matches just before end-of-string OR
# just before a trailing newline). \A/\Z anchor to the true string
# boundaries so correctness never depends on max_length happening to equal
# the valid string length.
HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"\A#[0-9A-Fa-f]{6}\Z",
    message="Enter a valid 6-digit hex color, e.g. #1877F2.",
)

# Doubt-driven-development finding: nothing stops an admin from
# accidentally creating an unbounded number of footer links, degrading the
# public footer on every page. Enforced in SocialMediaLinkForm.clean(), not
# here -- a create-time business rule, not a data-integrity constraint.
MAX_SOCIAL_MEDIA_LINKS = 20

SOCIAL_MEDIA_LINKS_CACHE_KEY = "pages:social_media_links"


class SocialMediaLink(models.Model):
    name = models.CharField(max_length=50)
    url = models.URLField(max_length=500)
    platform = models.CharField(
        max_length=20, choices=SOCIAL_ICON_CHOICES, default="custom"
    )
    icon_color = models.CharField(max_length=7, validators=[HEX_COLOR_VALIDATOR])
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Doubt-driven-development finding: Task 15d hit this exact bug
        # once already (order_by("-created_at") with no tie-breaker could
        # skip/duplicate a row across a page boundary) -- "pk" as a
        # secondary sort key from the start, not added later as a fix.
        ordering = ["order", "pk"]

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return get_icon(self.platform)


@receiver(post_save, sender=SocialMediaLink)
@receiver(post_delete, sender=SocialMediaLink)
def _invalidate_social_media_links_cache(sender, **kwargs):
    cache.delete(SOCIAL_MEDIA_LINKS_CACHE_KEY)
