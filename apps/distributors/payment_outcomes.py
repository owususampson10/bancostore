"""Task 68c. What happened to one Paystack payment, for all three entry
points (registration fee, starter pack, storefront order).

Task 67 gave the registration path an outcome so its webhook could answer
503 -- "we could not verify this, send it again" -- instead of 200, which
tells Paystack the one push it guarantees was delivered and nothing more is
needed. The other two paths returned None whatever happened, so a 10-second
Paystack timeout burned that push and the payment waited for the next daily
reconciliation instead.

One enum for all three rather than one per app: the webhook dispatches all
three through the same dict, and the distinction that matters to it -- "did
this definitely resolve, or do we still not know?" -- is identical for a
registration fee, a starter pack and an order.
"""

from enum import Enum


class PaymentOutcome(str, Enum):
    # The payment did what it was for: account created, pack applied, order
    # confirmed.
    APPLIED = "applied"
    # A replay of the reference that already did it. Nothing to do.
    ALREADY_APPLIED = "already_applied"
    # Paystack says this was not paid (abandoned, failed, reversed, or a
    # reference it has never seen).
    NOT_PAID = "not_paid"
    # We could not get an answer. The caller must NOT treat this as
    # resolved: the webhook asks Paystack to retry, cleanup keeps the row.
    VERIFY_FAILED = "verify_failed"
    # Paid, but it could not be applied, and a PaymentIssue now records why
    # so the admin can refund or fix it.
    ISSUE_RECORDED = "issue_recorded"
    # Not a reference this platform issued.
    UNKNOWN_REFERENCE = "unknown_reference"
