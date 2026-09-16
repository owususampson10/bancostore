"""Task 62. Renders the order receipt as a PDF, for attaching to the email.

WHY AN ATTACHMENT AND NOT A DOWNLOAD LINK: an adversarial design review of
a public "download receipt" link found 13 problems, almost all caused by
the link needing a public URL -- an anonymous way to exhaust the site's
single Daphne process with PDF renders, a leaked SECRET_KEY exposing every
order's home address via sequential primary keys, no per-link revocation,
bearer credentials landing in access logs, and WeasyPrint (with an open
advisory) exposed to unauthenticated traffic. An attachment has no URL.

THREE HAZARDS THIS MODULE IS BUILT AROUND:

1. WeasyPrint cannot load on this project's dev Mac (no Pango), and a
   SECOND failed import in one process segfaulted the interpreter (Task
   45). So the library is loaded once per process and a failure is never
   retried. Callers get None and send the email without a PDF.

2. PYSEC-2026-3940 (weasyprint 69.0, fixed in 70.0): write_pdf()'s
   `stylesheets` and `xmp_metadata` options ignore the url_fetcher, giving
   SSRF and arbitrary local file read. Neither is used here -- all styling
   is inline in the template -- and a test enforces they never are.

3. The PDF is built from customer-typed names and addresses. Those are
   autoescaped by the template, so no markup gets in. The restricted
   url_fetcher is the second line of defence: it serves exactly one file,
   the logo, and refuses every other URL whatever asked for it.
"""

import importlib
import logging
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from .receipts import build_receipt_html_context

logger = logging.getLogger(__name__)

# The logo is read from DISK, never fetched over the network. Reusing the
# email's hosted https URL would make the worker fetch its own logo back
# through the public Nginx on every render -- slow, and silently missing
# whenever DNS, TLS or Nginx misbehaves (a design review finding).
RECEIPT_LOGO_FILE = (
    Path(settings.BASE_DIR)
    / "static"
    / "images"
    / "bancostore-brand"
    / "logo"
    / "bancostore-logo-orange-email.png"
)
RECEIPT_LOGO_URI = RECEIPT_LOGO_FILE.as_uri()

_UNSET = object()
_weasyprint = _UNSET


def _reset_weasyprint_cache():
    """Test hook only: forget a cached load result."""
    global _weasyprint
    _weasyprint = _UNSET


def _load_weasyprint():
    """The weasyprint module, or None if it cannot load in this process.

    Memoised INCLUDING the failure. On the dev Mac a second failed import
    in the same process segfaulted the interpreter (Task 45) -- a long-
    running local server confirming several orders would hit that, so the
    first failure is remembered and never retried.

    Missing Pango raises OSError from WeasyPrint's own __init__, not
    ImportError (see tests/conftest.py), so both are caught.
    """
    global _weasyprint
    if _weasyprint is _UNSET:
        try:
            _weasyprint = importlib.import_module("weasyprint")
        except (ImportError, OSError):
            logger.warning(
                "receipt_pdf: WeasyPrint could not load in this process; receipt "
                "emails will be sent without a PDF attachment. Expected on the "
                "local dev Mac, never on the production server."
            )
            _weasyprint = None
    return _weasyprint


def make_receipt_url_fetcher(response_cls):
    """A WeasyPrint url_fetcher that serves only the local logo file.

    The response class is passed in rather than imported, so this can be
    tested without importing WeasyPrint (see _load_weasyprint).

    Refusing by raising is safe: WeasyPrint wraps any fetcher exception in
    URLFetchingError, which images.py catches and logs as "Failed to load
    image" before carrying on rendering -- verified in weasyprint/urls.py
    and weasyprint/images.py for 69.0. So a refused resource fails closed:
    no image, but still a PDF.

    Returns a URLFetcherResponse rather than a dict, which 69.0 deprecates.
    """

    def fetch(url):
        if url != RECEIPT_LOGO_URI:
            raise ValueError(f"receipt PDF refused to load resource: {url!r}")
        return response_cls(
            url,
            body=RECEIPT_LOGO_FILE.read_bytes(),
            headers={"Content-Type": "image/png"},
        )

    return fetch


def receipt_pdf_filename(order) -> str:
    """e.g. bancostore-receipt-2026-09-16-95f556e7.pdf

    The "order-" prefix is stripped first. A design review caught that
    "the first 8 characters of the reference" is always "order-XY" --
    only 256 possible names, colliding in the customer's Downloads folder.
    """
    from django.utils import timezone

    date = timezone.localtime(order.created_at).strftime("%Y-%m-%d")
    short = order.payment_reference.removeprefix("order-")[:8]
    return f"bancostore-receipt-{date}-{short}.pdf"


def render_receipt_pdf(order, intro="", closing=""):
    """PDF bytes for the order's receipt, or None if WeasyPrint is
    unavailable in this process.

    Reuses the email's own HTML template rather than a second print
    template, so the PDF and the email can never drift apart visually. Only
    the logo source differs: a local file URI instead of the hosted URL.

    `intro` and `closing` are the admin-editable wording the email already
    rendered, passed straight through so the attached PDF says exactly what
    the email body says. The PDF has no editable wording of its own.

    Deliberately NOT passing `stylesheets` or `xmp_metadata` to write_pdf()
    -- see PYSEC-2026-3940 in the module docstring. A test enforces this.
    """
    weasyprint = _load_weasyprint()
    if weasyprint is None:
        return None

    context = build_receipt_html_context(order)
    context["logo_src"] = RECEIPT_LOGO_URI
    context["intro"] = intro
    context["closing"] = closing
    # Switches on the template's print rules: A5 paper, a white page instead
    # of the email's grey backdrop, and a full-width card. Found necessary by
    # rendering the real PDF on the production server -- see the template.
    context["pdf"] = True
    html = render_to_string("emails/order_receipt.html", context)

    fetcher = make_receipt_url_fetcher(weasyprint.urls.URLFetcherResponse)
    return weasyprint.HTML(string=html, url_fetcher=fetcher).write_pdf()
