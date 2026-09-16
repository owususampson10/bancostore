"""Task 60. Builds the customer-facing order confirmation receipt.

Kept out of services.py deliberately: that module owns the money, stock
and PV side of an order's lifecycle, and none of this affects any of
them. A receipt is presentation.

Every value here is read from the Order's own snapshotted fields (Task
17a/17c), never from a live Product or constance setting -- a price
change, a product rename, or a delivery-fee edit after the fact must
never rewrite what an already-sent receipt said.
"""

from django.utils import timezone

# Task 60: apps.notifications.rendering substitutes a {{name}} token with
# str(value) from a fixed context dict and has NO loop construct -- that
# is a deliberate security property (see its module docstring: an
# admin-authored template string must never be able to reach an attribute
# or execute code, which str.format would allow). So the per-line item
# list cannot be a loop inside the template; it is pre-rendered here into
# one plain string and passed as a single {{items}} value. An admin can
# move that block and reword everything around it, but not restructure
# the line format itself. That limit is the cost of the security
# property, and is cheaper than making the renderer more powerful.
_ITEM_LINE = "  {quantity} x {name}  --  GHS {line_total}"


def render_items_block(order) -> str:
    """One line per order line: quantity, name, and the LINE total (unit
    price x quantity), not the unit price on its own.

    Reads OrderItem.product_name, the snapshot taken at order creation --
    never item.product.name, which can have been renamed since. (The FK
    itself is PROTECT, so the product row cannot vanish underneath an
    order; the snapshot is about renames, not deletion.)
    """
    return "\n".join(
        _ITEM_LINE.format(
            quantity=item.quantity,
            name=item.product_name,
            line_total=f"{item.unit_price * item.quantity:.2f}",
        )
        for item in order.items.all()
    )


def _render_delivery_details(order) -> str:
    """The address block for a home delivery, or a plain statement for a
    pickup -- which carries no address at all (the model's own
    order_delivery_address_required_iff_home_delivery CheckConstraint
    enforces those fields are blank), so rendering the same block would
    leave a run of empty lines in a real customer's receipt.

    landmark stays optional even for home delivery, so a blank one is
    dropped rather than left as a dangling empty line.
    """
    from .models import Order

    if order.delivery_method == Order.DeliveryMethod.PICKUP:
        return "Pickup from our Bancostore location."

    lines = [
        f"Home delivery ({order.get_delivery_zone_display()}) to:",
        order.address,
        order.area,
        order.landmark,
        f"{order.full_name}, {order.phone_number}",
    ]
    return "\n".join(line for line in lines if line and str(line).strip())


def build_receipt_context(order) -> dict:
    """The fixed context dict handed to the notification renderer.

    Every value is a plain string: the renderer substitutes str(value),
    so a Decimal or a model instance leaking in would render as a repr in
    a real customer's receipt. Money is formatted to 2dp here rather than
    in the template, since an admin editing wording must not be able to
    change how an amount is rounded.
    """
    return {
        "customer_name": order.full_name,
        "reference": order.payment_reference,
        "order_date": timezone.localtime(order.created_at).strftime("%d %B %Y"),
        "items": render_items_block(order),
        "subtotal": f"{order.subtotal:.2f}",
        "discount_amount": f"{order.discount_amount:.2f}",
        "delivery_fee": f"{order.delivery_fee:.2f}",
        "total": f"{order.total:.2f}",
        "delivery_details": _render_delivery_details(order),
    }
