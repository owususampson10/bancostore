from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.catalog.models import Product


class ProductSitemap(Sitemap):
    protocol = "https"
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Product.objects.filter(is_active=True).only("slug", "updated_at")

    def location(self, product):
        return reverse("catalog:product_detail", args=[product.slug])

    def lastmod(self, product):
        return product.updated_at


class StaticViewSitemap(Sitemap):
    protocol = "https"
    # No lastmod -- these pages have no tracked "last changed" timestamp,
    # and a fabricated one (e.g. "now" on every crawl) would be actively
    # misleading to a search engine rather than merely absent.
    changefreq = "monthly"
    priority = 0.5

    def items(self):
        return [
            "catalog:home",
            "catalog:product_list",
            "pages:about",
            "pages:contact",
            "pages:terms_of_use",
            "pages:privacy_policy",
            "pages:cookie_policy",
            "pages:disclaimer",
            "pages:earnings_disclosure",
            "pages:ai_disclaimer",
            "pages:returns_refunds_shipping",
        ]

    def location(self, item):
        return reverse(item)


sitemaps = {
    "products": ProductSitemap,
    "static": StaticViewSitemap,
}
