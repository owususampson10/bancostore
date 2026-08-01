import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from constance import config

from apps.catalog.models import Product
from apps.distributors.paystack import PaystackError, initialize_transaction

from .cart import Cart
from .forms import CheckoutForm
from .models import Order
from .services import confirm_order_payment, create_pending_order, prefill_contact_info

logger = logging.getLogger(__name__)


def cart_view(request):
    cart = Cart(request)
    return render(
        request,
        "orders/cart.html",
        {"cart_items": cart.items(), "cart_subtotal": cart.subtotal()},
    )


@require_POST
def cart_add(request, product_id):
    product = get_object_or_404(Product.objects.storefront_visible(), pk=product_id)
    added = Cart(request).add(product, quantity=1)
    if not added:
        # code-review-and-quality (2026-07-24): stock ran out between page
        # render and submit -- redirecting to /cart/ as if this succeeded
        # would silently strand the customer with no feedback at all.
        # Sending them back to the product page (now showing accurate,
        # live stock) is a minimal fix; a real toast/message requires
        # wiring django.contrib.messages into base_store.html's shared
        # header, deliberately deferred as a separate, broader change.
        return redirect(reverse("catalog:product_detail", args=[product.slug]))
    return redirect("orders:cart")


@require_POST
def cart_update(request, product_id):
    # Plain manager, not storefront_visible() -- visibility gates adding
    # NEW items, not modifying/removing a line already in the cart.
    # code-review-and-quality (2026-07-24) caught storefront_visible() here
    # 404ing this action for a product deactivated after being added, with
    # no way for the customer to recover through the UI.
    product = get_object_or_404(Product, pk=product_id)
    try:
        quantity = int(request.POST.get("quantity", 1))
    except ValueError:
        quantity = 1
    Cart(request).update(product, quantity)
    return redirect("orders:cart")


@require_POST
def cart_remove(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    Cart(request).remove(product)
    return redirect("orders:cart")


def checkout_view(request):
    cart = Cart(request)
    # cart.items() itself is the source of truth here (it self-heals
    # stale/deactivated products, apps/orders/cart.py) -- an empty cart
    # can't check out, whether it started empty or a POST raced a
    # concurrent removal down to empty.
    cart_items = cart.items()
    if not cart_items:
        return redirect("orders:cart")

    payment_error = False
    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            order = create_pending_order(
                user=request.user,
                cart_items=cart_items,
                form_data=form.cleaned_data,
            )
            # Task 17d (ADR-0005 decision 3): Order creation and Paystack
            # initialization happen in the same step, exactly as the ADR
            # documents it -- there's no separate decision point between
            # confirming the order summary and being sent to pay.
            callback_url = request.build_absolute_uri(
                reverse("orders:order_payment_callback")
            )
            # Paystack requires an email on every transaction; Order.email
            # is optional (a guest may leave it blank) -- fall back to a
            # synthetic address that satisfies the API without claiming
            # it's a real contact channel, mirroring
            # pay_registration_fee's own established workaround. Never
            # persisted onto Order.email itself.
            email = order.email or f"{order.phone_number}@bancostore.test"
            try:
                data = initialize_transaction(
                    email=email,
                    amount_pesewas=int(order.total * 100),
                    reference=order.payment_reference,
                    callback_url=callback_url,
                )
            except PaystackError:
                logger.exception(
                    "checkout_view: Paystack initialize_transaction failed "
                    "for order=%s -- the order already exists as PENDING; "
                    "the customer can retry from this same page.",
                    order.payment_reference,
                )
                payment_error = True
            else:
                # CodeRabbit (PR #27): every other Paystack response read
                # in this codebase uses .get() so a malformed/unexpected
                # shape logs and returns rather than raising --
                # data["authorization_url"] was the one bracket-indexed
                # read, turning a missing key into an unhandled 500 after
                # the order already existed as PENDING.
                authorization_url = data.get("authorization_url")
                if authorization_url:
                    return redirect(authorization_url)
                logger.error(
                    "checkout_view: Paystack initialize_transaction "
                    "returned no authorization_url for order=%s",
                    order.payment_reference,
                )
                payment_error = True
    else:
        form = CheckoutForm(initial=prefill_contact_info(request.user))

    # security-and-hardening (2026-07-25): form.delivery_method.value /
    # form.delivery_zone.value echo back the RAW submitted POST value on a
    # bound-but-invalid form (Django's BoundField.value() does this
    # regardless of whether the field itself validated) -- interpolating
    # that directly into checkout.html's Alpine x-data JS string would be
    # the exact same XSS this codebase already found and fixed once in
    # apps/distributors/views.py::payout_settings (see the long comment
    # there): the browser HTML-decodes the attribute value BEFORE Alpine
    # evaluates it as JS, so Django's HTML auto-escaping does not protect
    # a JS-string-literal context. Fixed the same two ways: constrain to
    # known-good choices here, server-side, before the value ever reaches
    # the template, and pass it via json_script (never raw interpolation).
    valid_delivery_methods = dict(Order.DeliveryMethod.choices)
    valid_delivery_zones = dict(Order.DeliveryZone.choices)
    submitted_method = form.data.get("delivery_method") if form.is_bound else ""
    submitted_zone = form.data.get("delivery_zone") if form.is_bound else ""
    selected_delivery_method = (
        submitted_method
        if submitted_method in valid_delivery_methods
        else Order.DeliveryMethod.HOME_DELIVERY
    )
    selected_delivery_zone = (
        submitted_zone if submitted_zone in valid_delivery_zones else ""
    )

    cart_subtotal = sum((line.line_total for line in cart_items), start=Decimal("0"))
    return render(
        request,
        "orders/checkout.html",
        {
            "form": form,
            "cart_items": cart_items,
            "cart_subtotal": cart_subtotal,
            "payment_error": payment_error,
            # For the live delivery-fee preview only (Alpine, client-side)
            # -- the authoritative fee is always recomputed server-side by
            # calculate_delivery_fee() at submission, never trusted from
            # this preview.
            "delivery_fee_kumasi": config.DELIVERY_FEE_KUMASI,
            "delivery_fee_accra": config.DELIVERY_FEE_ACCRA,
            "delivery_fee_other_regions": config.DELIVERY_FEE_OTHER_REGIONS,
            "free_delivery_threshold": config.FREE_DELIVERY_THRESHOLD,
            "selected_delivery_method": selected_delivery_method,
            "selected_delivery_zone": selected_delivery_zone,
            # Custom-listbox choices for the Delivery Zone control (2026-07-25
            # design fix, matching payout_settings.html's mobile-money-network
            # listbox) -- passed via json_script in the template rather than
            # interpolated into Alpine's x-data string, same XSS reasoning as
            # that listbox's own selected/options.
            "delivery_zone_choices": [
                {"value": value, "label": label}
                for value, label in Order.DeliveryZone.choices
            ],
        },
    )


def order_confirmation_view(request, payment_reference):
    # Keyed by payment_reference (a 128-bit uuid4, effectively
    # unguessable), not by a session/account check -- consistent with how
    # this codebase already keys other post-checkout lookups by an
    # unguessable token rather than requiring the same session
    # (apps/distributors's PendingRegistration). No PII beyond what the
    # order's own creator already knows (total, reference) is shown here.
    #
    # code-review-and-quality (2026-07-25): order_created.html's confirmed
    # state iterates order.items.all and reads item.product.primary_image
    # per item -- Product.primary_image's own docstring requires a
    # caller's prefetch_related("images") to avoid a query per call, and
    # without prefetching items__product too, item.product itself would
    # also be a query per item. Both prefetched here to avoid an N+1.
    order = get_object_or_404(
        Order.objects.prefetch_related("items__product__images"),
        payment_reference=payment_reference,
    )
    # Split-view redesign (2026-08-01): only this one-time post-checkout
    # redirect shows the "View Order Status" button -- order_detail (the
    # permanent, revisitable page) reuses this same template without the
    # flag, since a "View Order Status" button pointing at the page
    # you're already on would be circular.
    return render(
        request,
        "orders/order_created.html",
        {"order": order, "is_confirmation": True},
    )


def order_payment_callback(request):
    """Task 17d. The customer's browser lands here after attempting
    payment on Paystack's hosted page. Mirrors
    apps.distributors.views.starter_pack_payment_callback's shape
    exactly: never trusts the callback's own query-string claims about
    payment status for anything beyond triggering the same server-side
    re-verification the webhook does -- confirm_order_payment always
    re-fetches and re-checks everything itself, and is a safe idempotent
    no-op if the webhook already won the race.

    CodeRabbit (PR #27) caught two real gaps in the original version:
    (1) reference was reversed into a URL unvalidated -- fully
    attacker-controlled query-string input, and reverse() raises
    NoReverseMatch (confirmed via manage.py shell) for any value
    containing "/", an unauthenticated 500 on this public GET endpoint.
    Fixed by resolving the Order first and redirecting to the cart if no
    match exists, the same way a missing/blank reference already did.
    (2) The session cart was never cleared once a payment actually
    confirmed, so the customer could immediately re-order the same
    items. Fixed by re-checking the order's status after
    confirm_order_payment runs and clearing the cart only then --
    confirm_order_payment itself has no request/session to clear."""
    reference = request.GET.get("reference", "")
    if not reference:
        return redirect("orders:cart")
    order = Order.objects.filter(payment_reference=reference).first()
    if order is None:
        return redirect("orders:cart")
    confirm_order_payment(reference)
    order.refresh_from_db()
    if order.status == Order.Status.CONFIRMED:
        Cart(request).clear()
    return redirect("orders:order_confirmation", reference)


@login_required(login_url="account_login")
def order_history(request):
    """Task 25: a logged-in user's own paginated purchase history --
    identically reachable for a regular customer or a distributor-as-
    customer, since Order.customer is a plain FK to settings.
    AUTH_USER_MODEL, not Distributor-specific. No id/param IDOR surface:
    always scoped to request.user, matching apps.distributors.views.
    earnings_history/payout_settings's own established convention.

    `customer=request.user` alone is sufficient to exclude guest-checkout
    orders (`customer=NULL`) -- SQL equality can never match NULL, so an
    additional `.exclude(customer__isnull=True)` would be dead code that
    only creates a false impression of a second defense layer (doubt-
    driven-development finding, pre-implementation review).

    `-created_at, -pk` ordering mirrors Task 15d's own pagination-
    stability fix (a timestamp collision with no tie-breaker could skip/
    duplicate a row across a page boundary). `get_page()`, not `.page()`
    -- clamps an invalid/out-of-range `?page=` value instead of raising,
    matching apps.distributors.views.earnings_history/withdrawal_history's
    own convention exactly.

    Prefetches `items__product__images` for the same reason
    order_confirmation_view already documents: Product.primary_image
    requires it to avoid a query per item, and without prefetching
    `items__product` too, `item.product` itself would be a query per
    item."""
    orders = (
        Order.objects.filter(customer=request.user)
        .prefetch_related("items__product__images")
        .order_by("-created_at", "-pk")
    )
    paginator = Paginator(orders, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    # Elided range (CodeRabbit): page_obj.paginator.page_range alone renders
    # every page number with no truncation -- fine at today's order volumes,
    # but unbounded for a customer with dozens of pages of history. Restored
    # 2026-08-01 after a first split-view pass wrongly dropped this in favor
    # of Previous/Next text -- the actual generated Stitch source uses
    # numbered pagination circles even in the narrower list column.
    page_range = paginator.get_elided_page_range(page_obj.number)
    return render(
        request,
        "orders/order_history.html",
        {"page_obj": page_obj, "page_range": page_range},
    )


@login_required(login_url="account_login")
def order_detail(request, pk):
    """Task 25: a NEW, ownership-scoped detail view -- deliberately not a
    reuse of order_confirmation_view. That view's "no ownership check,
    unguessable payment_reference token" design is safe only because
    it's reachable solely via a one-time post-checkout redirect, never
    rendered as a durable, revisitable link. Embedding that same token
    into every row of this permanent, bookmarkable history page would
    silently make the token the sole, forever-standing protection for a
    customer's name/phone/address -- a materially different threat model
    nobody actually decided to accept (doubt-driven-development finding,
    pre-implementation review). `pk=pk, customer=request.user` is IDOR-
    safe by construction and also correctly 404s a guest order's pk
    (customer=NULL can never match request.user).

    Reuses order_created.html -- already proven to never render
    Order.tracking_note (admin-only free text) or any other field this
    self-service page shouldn't show."""
    order = get_object_or_404(
        Order.objects.prefetch_related("items__product__images"),
        pk=pk,
        customer=request.user,
    )
    return render(request, "orders/order_created.html", {"order": order})
