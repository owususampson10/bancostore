from django import template

register = template.Library()


@register.filter
def page_url(querystring_no_page, page_number):
    """Builds a pagination link's querystring (e.g. "?q=foo&page=2"),
    preserving the active filters — product_grid.html's prev/number/next
    links and their matching hx-get attributes all need this exact string,
    so it's built once here instead of six times inline."""
    if querystring_no_page:
        return f"?{querystring_no_page}&page={page_number}"
    return f"?page={page_number}"
