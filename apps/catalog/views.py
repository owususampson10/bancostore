from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from constance import config

from apps.orders.models import Order, OrderItem
from apps.promotions.models import Banner
from bancostore.json_ld import dumps_for_script_tag

from .forms import ReviewForm
from .models import Category, Product, Review, WishlistItem
from .services import backorder_display_context, storefront_visible_products

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
        storefront_visible_products()
        .filter(is_featured=True)
        .order_by("-created_at")[:8]
    )
    context = {
        "featured_products": featured_products,
        "categories": Category.objects.all(),
        "active_banners": Banner.objects.active().select_related("product", "category"),
        **backorder_display_context(),
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

    products = storefront_visible_products()
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
        **backorder_display_context(),
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
    -- dumps_for_script_tag (bancostore/json_ld.py) is the single source of
    truth for correct escaping, both for valid JSON syntax and for safety
    inside a <script> element (a literal "</script>" in a product
    description could otherwise break out of the tag -- CodeRabbit finding
    on PR #70). "image" is only included when a real photo exists --
    claiming the generic OG banner is a photo of this specific product
    would be inaccurate structured data, unlike Open Graph where a generic
    social preview image is normal practice. Takes primary_image as an
    already-resolved argument (not re-read via product.primary_image)
    since the caller already needed it for the same purpose -- avoids a
    second walk of product.images.all()."""
    base_url = f"{request.scheme}://{request.get_host()}"
    primary_image = product.primary_image
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
    if primary_image:
        data["image"] = base_url + primary_image.image.url
    return dumps_for_script_tag(data)


def _can_review(user, product):
    """Task 41a. Eligibility is a delivered order containing this product
    for this user -- Order.status == "delivered" (Task 18) is the real
    signal the source doc asks for ("after receiving their order"), not
    just having purchased it."""
    return (
        user.is_authenticated
        and OrderItem.objects.filter(
            order__customer=user,
            order__status=Order.Status.DELIVERED,
            product=product,
        ).exists()
    )


def _product_detail_context(request, product, review_form=None):
    related_products = (
        storefront_visible_products()
        .filter(category=product.category)
        .exclude(pk=product.pk)[:4]
    )
    in_wishlist = (
        request.user.is_authenticated
        and WishlistItem.objects.filter(user=request.user, product=product).exists()
    )
    can_review = _can_review(request.user, product)
    if review_form is None:
        existing_review = (
            Review.objects.filter(user=request.user, product=product).first()
            if can_review
            else None
        )
        review_form = ReviewForm(instance=existing_review)
    # Task 41b: approved reviews only, both for the list and the average --
    # an unapproved review must never leak into the public average rating
    # either, not just the visible list.
    approved_reviews = (
        Review.objects.filter(product=product, is_approved=True)
        .select_related("user")
        .order_by("-created_at")
    )
    average_rating = approved_reviews.aggregate(avg=Avg("rating"))["avg"]
    if average_rating is not None:
        average_rating = round(average_rating, 1)
    return {
        "product": product,
        "related_products": related_products,
        "product_json_ld": _build_product_json_ld(request, product),
        "in_wishlist": in_wishlist,
        "can_review": can_review,
        "review_form": review_form,
        "reviews": approved_reviews,
        "average_rating": average_rating,
        **backorder_display_context(),
    }


def product_detail(request, slug):
    # Task 44a: storefront_visible_products() -- 404s a hidden
    # out-of-stock product ("hide" mode means gone everywhere, including a
    # direct link to its own page, not just absent from listings).
    product = get_object_or_404(
        storefront_visible_products().prefetch_related("variants", "images"),
        slug=slug,
    )
    return render(
        request,
        "catalog/product_detail.html",
        _product_detail_context(request, product),
    )


@login_required(login_url="account_login")
@require_POST
def review_submit(request, product_id):
    """Task 41a/41b. update_or_create-shaped via instance= (not a separate
    add/edit endpoint) -- one review per customer per product (confirmed
    with the user), so a resubmission always edits the existing row in
    place. is_approved is always re-derived from the live
    PRODUCT_REVIEW_AUTO_APPROVE_ENABLED setting on every (re)submission,
    never carried over from the previous value: under Manual (the
    default) that means unapproved every time, so a materially different
    review body/rating can never stay silently approved with
    never-moderated content."""
    product = get_object_or_404(Product.objects.storefront_visible(), pk=product_id)
    if not _can_review(request.user, product):
        raise PermissionDenied
    existing_review = Review.objects.filter(user=request.user, product=product).first()
    form = ReviewForm(request.POST, instance=existing_review)
    if form.is_valid():
        review = form.save(commit=False)
        review.user = request.user
        review.product = product
        # Task 41b: the 13.10 "Product Review Approval" admin toggle --
        # Manual (the default) means every (re)submission always goes back
        # to unapproved for review; Auto-approve skips that entirely.
        review.is_approved = config.PRODUCT_REVIEW_AUTO_APPROVE_ENABLED
        try:
            with transaction.atomic():
                review.save()
        except IntegrityError:
            # code-review-and-quality finding: existing_review was None at
            # the query above, but a concurrent submission from the same
            # user for the same product won the race and inserted its own
            # row first -- Review's own UniqueConstraint(user, product)
            # turns the second INSERT into an IntegrityError instead of a
            # silent second row. Fall back to updating the row that won,
            # matching wishlist_toggle's own get_or_create precedent for
            # this exact class of race, rather than a 500.
            review = Review.objects.get(user=request.user, product=product)
            review.rating = form.cleaned_data["rating"]
            review.body = form.cleaned_data["body"]
            review.is_approved = config.PRODUCT_REVIEW_AUTO_APPROVE_ENABLED
            review.save()
        if review.is_approved:
            messages.success(request, "Thanks for your review! It's now live.")
        else:
            messages.success(
                request, "Thanks for your review! It will appear once approved."
            )
        return redirect("catalog:product_detail", slug=product.slug)
    return render(
        request,
        "catalog/product_detail.html",
        _product_detail_context(request, product, review_form=form),
    )


@login_required(login_url="account_login")
@require_POST
def wishlist_toggle(request, product_id):
    """Task 39. A single toggle endpoint (not separate add/remove routes)
    matches the product page's single heart-icon button. get_or_create
    plus the model's own UniqueConstraint makes a rapid double-submit
    idempotent rather than a 500. Always redirects back to the product
    page it came from -- no `next` query param accepted, closing off the
    open-redirect risk Task 17d's own CodeRabbit finding already flagged
    for this exact class of "reverse an attacker-controlled value" bug."""
    product = get_object_or_404(Product.objects.storefront_visible(), pk=product_id)
    item, created = WishlistItem.objects.get_or_create(
        user=request.user, product=product
    )
    if not created:
        item.delete()
    return redirect("catalog:product_detail", slug=product.slug)


@login_required(login_url="account_login")
def wishlist_view(request):
    items = (
        WishlistItem.objects.filter(user=request.user)
        .select_related("product", "product__category")
        .prefetch_related("product__images")
    )
    # Task 44a follow-up (CodeRabbit, PR #75): OUT_OF_STOCK_BEHAVIOUR="hide"
    # means a product is gone everywhere (storefront_visible_products()
    # already enforces this for listing/search/home/detail) -- without this,
    # a hidden product saved to a wishlist before it went out of stock kept
    # showing a tile whose own detail link 404s.
    if config.OUT_OF_STOCK_BEHAVIOUR == "hide":
        items = items.exclude(product__stock=0)
    return render(
        request,
        "catalog/wishlist.html",
        {"items": items, **backorder_display_context()},
    )
