from decimal import Decimal

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from constance import config

from apps.catalog.models import Product

from .cart import Cart
from .forms import CheckoutForm
from .models import Order
from .services import create_pending_order, prefill_contact_info


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

    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            order = create_pending_order(
                user=request.user,
                cart_items=cart_items,
                form_data=form.cleaned_data,
            )
            # code-review-and-quality (2026-07-25): Post/Redirect/Get --
            # rendering order_created.html directly as the POST response
            # meant an ordinary page refresh on the confirmation screen
            # resubmitted the form and created a duplicate pending Order.
            # Redirecting to a GET view keyed by the order's own
            # unguessable payment_reference closes off the single most
            # common way a customer stumbles into that.
            return redirect("orders:order_confirmation", order.payment_reference)
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
    order = get_object_or_404(Order, payment_reference=payment_reference)
    return render(request, "orders/order_created.html", {"order": order})
