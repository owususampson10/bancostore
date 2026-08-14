from decimal import Decimal

import pytest

from apps.orders.models import Order
from bancostore.exports import csv_safe_cell, export_as_csv, export_as_pdf
from tests.conftest import WEASYPRINT_AVAILABLE

# ---------------------------------------------------------------------------
# csv_safe_cell
# ---------------------------------------------------------------------------


def test_csv_safe_cell_passes_through_an_ordinary_value():
    assert csv_safe_cell("Ama Mensah") == "Ama Mensah"


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_csv_safe_cell_neutralizes_every_owasp_formula_trigger_character(trigger):
    """OWASP CSV injection: a cell value starting with any of =, +, -, @
    is interpreted as a live formula by Excel/Sheets on open."""
    value = f'{trigger}HYPERLINK("https://evil.example")'

    result = csv_safe_cell(value)

    assert result == f"'{value}"


def test_csv_safe_cell_catches_a_trigger_character_after_leading_whitespace():
    """A formula can be padded with whitespace to dodge a naive
    startswith check -- must be caught after stripping, not before."""
    result = csv_safe_cell("  =2+2")

    assert result == "'  =2+2"


def test_csv_safe_cell_stringifies_non_string_values():
    assert csv_safe_cell(Decimal("450.00")) == "450.00"
    assert csv_safe_cell(42) == "42"


def test_csv_safe_cell_neutralizes_a_negative_decimal():
    """A negative number's string form starts with "-", one of the same
    OWASP trigger characters -- correctly caught even though the value
    itself is legitimate data, matching this codebase's existing
    established precedent (E.164 phone numbers all start with "+")."""
    assert csv_safe_cell(Decimal("-5.00")) == "'-5.00"


# ---------------------------------------------------------------------------
# export_as_csv
# ---------------------------------------------------------------------------


def test_export_as_csv_sets_content_type_and_filename():
    response = export_as_csv("widgets.csv", ["Name"], [["Widget"]])

    assert response["Content-Type"] == "text/csv"
    assert response["Content-Disposition"] == 'attachment; filename="widgets.csv"'


def test_export_as_csv_writes_the_header_row_first():
    response = export_as_csv(
        "widgets.csv", ["Name", "Price"], [["Widget", Decimal("9.99")]]
    )

    lines = response.content.decode().splitlines()
    assert lines[0] == "Name,Price"


def test_export_as_csv_writes_every_data_row_in_order():
    response = export_as_csv(
        "widgets.csv",
        ["Name"],
        [["First"], ["Second"], ["Third"]],
    )

    lines = response.content.decode().splitlines()
    assert lines[1:] == ["First", "Second", "Third"]


def test_export_as_csv_neutralizes_formula_injection_in_a_data_cell():
    """Every cell is run through csv_safe_cell -- a caller must never
    have to remember which specific columns are "risky" free text."""
    response = export_as_csv(
        "widgets.csv", ["Name"], [['=HYPERLINK("https://evil.example")']]
    )

    body = response.content.decode()
    assert "'=HYPERLINK" in body


def test_export_as_csv_handles_an_empty_row_set():
    response = export_as_csv("widgets.csv", ["Name"], [])

    lines = response.content.decode().splitlines()
    assert lines == ["Name"]


# ---------------------------------------------------------------------------
# export_as_pdf
# ---------------------------------------------------------------------------


def _make_order():
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference="order-test-exports-ref",
        status=Order.Status.CONFIRMED,
    )


@pytest.mark.django_db
@pytest.mark.skipif(
    not WEASYPRINT_AVAILABLE,
    reason=(
        "WeasyPrint needs the system Pango library, not installable on "
        "this Mac (macOS 12 is an unsupported Homebrew Tier-3 config) -- "
        "verified for real in CI instead, where Pango installs via apt. "
        "See project_weasyprint_pango_blocked_locally memory."
    ),
)
def test_export_as_pdf_sets_content_type_and_filename():
    order = _make_order()

    response = export_as_pdf(
        "receipt.pdf",
        "admin_portal/order_invoice.html",
        {"order": order},
    )

    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'inline; filename="receipt.pdf"'


@pytest.mark.django_db
@pytest.mark.skipif(not WEASYPRINT_AVAILABLE, reason="Pango not installed locally")
def test_export_as_pdf_returns_real_pdf_bytes():
    order = _make_order()

    response = export_as_pdf(
        "receipt.pdf",
        "admin_portal/order_invoice.html",
        {"order": order},
    )

    assert response.content.startswith(b"%PDF")
