from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.catalog.models import Product

from .cart import Cart


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
