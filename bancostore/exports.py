"""Shared CSV/PDF export helpers (Task 45). Extracted so every admin_portal
report (Task 46/47) and the existing GRA withholding-tax export screen reuse
one implementation instead of each hand-rolling its own -- matching this
codebase's own repeated "extract shared, don't duplicate" convention (Task
19a's reverse_ancestor_pv extraction, Task 33's walk_sponsor_chain_downline_ids
extraction).
"""

import csv

from django.http import HttpResponse
from django.template.loader import render_to_string


def csv_safe_cell(value):
    """Neutralizes CSV formula injection (OWASP): a cell value starting
    with =, +, -, or @ (after stripping leading whitespace, since a
    formula can be padded to dodge a naive startswith check) is
    interpreted as a live formula the moment the file is opened in
    Excel/Sheets. Applied unconditionally to every cell export_as_csv
    writes -- a caller never has to remember which specific columns are
    "risky" free text (an admin-entered name) versus "safe" controlled
    values (a status label) versus a value that only coincidentally
    starts with a trigger character (a negative amount, an E.164 phone
    number). Extracted from apps.admin_portal.views's original
    per-view `_csv_safe` (Task 23 follow-up)."""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def export_as_csv(filename, header_row, rows):
    """Returns an HttpResponse of type text/csv, attachment-disposed as
    `filename`. `rows` is an iterable of iterables (e.g. a list of lists
    or tuples) -- every cell, header row included, is passed through
    csv_safe_cell before being written, regardless of type
    (str/Decimal/int/etc., all stringified by csv_safe_cell itself).
    Every current caller passes a hardcoded, safe header (a literal list
    of column names), but this is a general-purpose shared utility --
    sanitizing the header too (CodeRabbit, PR #76) means a future caller
    that ever builds a header dynamically doesn't inherit an
    injection-shaped gap for free."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([csv_safe_cell(cell) for cell in header_row])
    for row in rows:
        writer.writerow([csv_safe_cell(cell) for cell in row])
    return response


def export_as_pdf(filename, template_name, context):
    """Returns an HttpResponse of type application/pdf, inline-disposed
    as `filename`, rendered from `template_name`/`context` via
    WeasyPrint. WeasyPrint is imported lazily, inside this function, not
    at module level: its own __init__ eagerly dlopen()s the system Pango
    library at import time, which isn't installed on this project's local
    dev Mac (macOS 12, an unsupported Homebrew Tier-3 configuration) --
    a module-level import here would break every other caller of this
    module on any machine missing Pango. Verified for real in CI instead,
    where Pango installs cleanly via apt (.github/workflows/ci.yml).
    Mirrors apps.admin_portal.views.order_invoice_pdf's own established
    pattern (Task 18e) exactly, extracted here so a second PDF export
    doesn't have to re-derive it. API confirmed against WeasyPrint's own
    docs: HTML(string=...).write_pdf() returns PDF bytes with no
    arguments.
    Source: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html
    """
    html_string = render_to_string(template_name, context)

    from weasyprint import HTML

    pdf_bytes = HTML(string=html_string).write_pdf()

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
