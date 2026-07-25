from collections import namedtuple
from decimal import Decimal

from apps.catalog.models import Product

CartLine = namedtuple("CartLine", ["product", "quantity", "line_total"])


class Cart:
    """Task 17b (ADR-0005 decision 2, corrected -- keyed by product id, not
    variant id, see the ADR's own correction note): session-backed,
    {product_id: quantity} -- identical for guest and logged-in visitors,
    no Cart/CartItem DB model. Prices are always read live off Product,
    never snapshotted -- unlike Order/OrderItem (created at checkout,
    17c/17a), a cart is pre-purchase and should reflect current
    prices/stock, not a frozen figure.

    code-review-and-quality (2026-07-24): this class does a read-modify-
    write against the whole session dict (read in __init__, write in
    _save()) with no compare-and-swap -- two concurrent mutating requests
    from the same session (two tabs, a double-click) can lose one update
    (last save wins). Accepted as a bounded risk, not a money bug: nothing
    here is the source of truth (checkout, 17c/17d, revalidates stock and
    price against Product directly), so the worst case is a lost add/
    update the customer can just retry -- not an over-sell or a wrong
    charge. Not fixed with session locking, which would be real complexity
    for a low-stakes, low-frequency race.
    """

    SESSION_KEY = "cart"

    def __init__(self, request):
        self.session = request.session
        self._data = self.session.get(self.SESSION_KEY, {})

    def _save(self):
        self.session[self.SESSION_KEY] = self._data
        self.session.modified = True

    def add(self, product, quantity=1) -> bool:
        """Returns False if nothing was added (e.g. the product has zero
        available stock) so the caller can tell a real add apart from a
        silent no-op, rather than always redirecting as if it succeeded."""
        current = self._data.get(str(product.pk), 0)
        new_quantity = min(current + quantity, product.stock)
        if new_quantity <= current:
            return False
        self._data[str(product.pk)] = new_quantity
        self._save()
        return True

    def update(self, product, quantity):
        # CodeRabbit (PR #25): must check the POST-cap quantity, not the
        # raw param -- a product gone out of stock (or deactivated) since
        # being added has product.stock=0, so an update(product, 5) would
        # otherwise store a zero-quantity line instead of removing it.
        new_quantity = min(quantity, product.stock)
        if new_quantity < 1:
            self.remove(product)
            return
        self._data[str(product.pk)] = new_quantity
        self._save()

    def remove(self, product):
        if self._data.pop(str(product.pk), None) is not None:
            self._save()

    def clear(self):
        # CodeRabbit (PR #27, Task 17d): called once a purchase actually
        # confirms (apps/orders/views.py::order_payment_callback) -- left
        # unfixed, the just-purchased items stayed in the cart and the
        # customer could immediately re-order them.
        self._data = {}
        self._save()

    def items(self):
        if not self._data:
            return []
        # prefetch_related("images"), not select_related("category") --
        # code-review-and-quality (2026-07-24) caught the original version
        # prefetching the wrong relation: cart.html reads
        # item.product.primary_image (which queries .images.all() per
        # product with no prefetch, a real N+1 -- empirically confirmed
        # 6 queries for 5 cart lines) but never reads item.product.category.
        # is_active=True -- doubt-driven-development (Task 17c design
        # review): a product deactivated (not just deleted) after being
        # added to a cart is just as unpurchasable; without this filter
        # it would silently keep resolving here, past both this self-heal
        # and, worse, into a real snapshotted OrderItem at checkout.
        products = list(
            Product.objects.filter(
                pk__in=self._data.keys(), is_active=True
            ).prefetch_related("images")
        )

        # Real bug report (2026-07-24): a product deleted after being
        # added left a permanent stale entry -- this query already
        # excludes it (nothing matches a deleted pk, or now a deactivated
        # one), but the session dict itself was never updated, so
        # count() (which never queries the DB, by design -- it's read on
        # every page via the header badge context processor) kept
        # including it forever. The cart page rendered empty while the
        # badge claimed otherwise, with nothing to ever self-correct it.
        # Pruning here means visiting the cart page heals the session;
        # count() stays cheap (no DB query) everywhere else.
        resolved_ids = {str(product.pk) for product in products}
        stale_ids = self._data.keys() - resolved_ids
        if stale_ids:
            for stale_id in stale_ids:
                del self._data[stale_id]
            self._save()

        return [
            CartLine(
                product=product,
                quantity=self._data[str(product.pk)],
                line_total=product.price * self._data[str(product.pk)],
            )
            for product in products
        ]

    def subtotal(self):
        return sum((line.line_total for line in self.items()), start=Decimal("0"))

    def count(self):
        return sum(self._data.values())
