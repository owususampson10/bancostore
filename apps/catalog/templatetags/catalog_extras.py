from django import template

from apps.catalog.services import is_backorder_eligible as _is_backorder_eligible

register = template.Library()


@register.filter
def is_backorder_eligible(product):
    """Task 44a: templates/orders/cart.html needs this per cart line to
    stop showing "Only 0 left in stock" and disabling the quantity-increase
    button for a backorder-eligible item -- Cart.add()/update() already
    allow the quantity to exceed stock, but the template's own stock
    check didn't know that, silently blocking the one thing this feature
    is for."""
    return _is_backorder_eligible(product)


@register.filter
def page_url(querystring_no_page, page_number):
    """Builds a pagination link's querystring (e.g. "?q=foo&page=2"),
    preserving the active filters — product_grid.html's prev/number/next
    links and their matching hx-get attributes all need this exact string,
    so it's built once here instead of six times inline."""
    if querystring_no_page:
        return f"?{querystring_no_page}&page={page_number}"
    return f"?page={page_number}"
