import json
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from .models import Category, Product

PRODUCTS_PER_PAGE = 9

# Task 8 decision (2026-07-12, tasks/todo.md): "popular" has no order data
# until Task 17 exists, so it's interim-backed by is_featured then newest.
# Swap this ordering for a real sales-count annotation once orders exist —
# the UI option itself doesn't change, only what's behind it.
SORT_OPTIONS = {
    "newest": ("-created_at",),
    "price_asc": ("price",),
    "price_desc": ("-price",),
    "popular": ("-is_featured", "-created_at"),
}
DEFAULT_SORT = "newest"


def home(request):
    featured_products = (
        Product.objects.storefront_visible()
        .filter(is_featured=True)
        .order_by("-created_at")[:8]
    )
    context = {
        "featured_products": featured_products,
        "categories": Category.objects.all(),
    }
    return render(request, "catalog/home.html", context)


def _parse_price(value):
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def product_list(request):
    query = request.GET.get("q", "").strip()
    category_slug = request.GET.get("category", "").strip()
    min_price = _parse_price(request.GET.get("min_price"))
    max_price = _parse_price(request.GET.get("max_price"))
    sort = request.GET.get("sort", DEFAULT_SORT)
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT

    products = Product.objects.storefront_visible()
    if query:
        products = products.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    if category_slug:
        products = products.filter(category__slug=category_slug)
    if min_price is not None:
        products = products.filter(price__gte=min_price)
    if max_price is not None:
        products = products.filter(price__lte=max_price)
    products = products.order_by(*SORT_OPTIONS[sort])

    paginator = Paginator(products, PRODUCTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)
    querystring_no_page = params.urlencode()

    context = {
        "page_obj": page_obj,
        "categories": Category.objects.all(),
        "query": query,
        "category_slug": category_slug,
        "min_price": request.GET.get("min_price", ""),
        "max_price": request.GET.get("max_price", ""),
        "sort": sort,
        "querystring_no_page": querystring_no_page,
    }

    template = (
        "catalog/partials/product_grid.html"
        if request.htmx
        else "catalog/product_list.html"
    )
    return render(request, template, context)


def _build_product_json_ld(request, product):
    """Task 37c: serialized here rather than built with template tags, so
    a product name/description can never produce broken or unescaped JSON
    -- json.dumps is the single source of truth for correct escaping,
    matching organization_json_ld's own reasoning (apps/pages/context_processors.py).
    "image" is only included when a real photo exists -- claiming the
    generic OG banner is a photo of this specific product would be
    inaccurate structured data, unlike Open Graph where a generic social
    preview image is normal practice."""
    base_url = f"{request.scheme}://{request.get_host()}"
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.name,
        "description": product.description,
        "offers": {
            "@type": "Offer",
            "url": base_url + request.path,
            "priceCurrency": "GHS",
            "price": str(product.price),
            "availability": (
                "https://schema.org/InStock"
                if product.in_stock
                else "https://schema.org/OutOfStock"
            ),
        },
    }
    if product.primary_image:
        data["image"] = base_url + product.primary_image.image.url
    return json.dumps(data)


def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.storefront_visible().prefetch_related("variants"),
        slug=slug,
    )
    related_products = (
        Product.objects.storefront_visible()
        .filter(category=product.category)
        .exclude(pk=product.pk)[:4]
    )
    return render(
        request,
        "catalog/product_detail.html",
        {
            "product": product,
            "related_products": related_products,
            "product_json_ld": _build_product_json_ld(request, product),
        },
    )
