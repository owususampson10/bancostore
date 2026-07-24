from .cart import Cart


def cart_count(request):
    """Task 17b: makes the header cart badge (templates/base_store.html)
    correct on every storefront page, not just cart-related views.
    Cart.count() reads only session data -- no DB query."""
    return {"cart_count": Cart(request).count()}
