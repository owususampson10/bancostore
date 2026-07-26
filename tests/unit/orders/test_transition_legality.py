import pytest

from apps.orders.models import Order
from apps.orders.services import is_legal_order_status_transition

Status = Order.Status


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        # System-automatic (already built, Task 17c/17d) -- included here
        # so the legality graph stays the single source of truth for
        # every transition, not just the ones Task 18 adds.
        (Status.PENDING, Status.CONFIRMED),
        # Auto-cancel unpaid orders (Task 18d) -- unpaid only, per
        # ADR-0006 decision 6.
        (Status.PENDING, Status.CANCELLED),
        # Admin-driven, pre-dispatch (Task 18c).
        (Status.CONFIRMED, Status.PROCESSING),
        (Status.PROCESSING, Status.DISPATCHED),
        (Status.DISPATCHED, Status.DELIVERED),
        # Cancellation is legal from every pre-dispatch, paid-or-unpaid
        # status per the source doc's own "cancelled before dispatch"
        # wording (Task 18b for the paid ones, 18d for pending).
        (Status.CONFIRMED, Status.CANCELLED),
        (Status.PROCESSING, Status.CANCELLED),
        # Refund has no timing restriction in the source doc -- legal
        # from any paid status onward (Task 18b).
        (Status.CONFIRMED, Status.REFUNDED),
        (Status.PROCESSING, Status.REFUNDED),
        (Status.DISPATCHED, Status.REFUNDED),
        (Status.DELIVERED, Status.REFUNDED),
    ],
)
def test_every_legal_transition_in_the_adr_table_is_accepted(from_status, to_status):
    assert is_legal_order_status_transition(from_status, to_status) is True


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        # Skipping a stage.
        (Status.PENDING, Status.PROCESSING),
        (Status.CONFIRMED, Status.DISPATCHED),
        (Status.CONFIRMED, Status.DELIVERED),
        # Moving backward.
        (Status.PROCESSING, Status.CONFIRMED),
        (Status.DISPATCHED, Status.PROCESSING),
        (Status.DELIVERED, Status.DISPATCHED),
        # Cancelling after dispatch -- the source doc's own "before
        # dispatch" restriction, the one CANCELLED can't cross that
        # REFUNDED deliberately can.
        (Status.DISPATCHED, Status.CANCELLED),
        (Status.DELIVERED, Status.CANCELLED),
        # Pending can't be refunded -- nothing has been paid yet.
        (Status.PENDING, Status.REFUNDED),
        # Terminal statuses have no legal transitions out.
        (Status.CANCELLED, Status.CONFIRMED),
        (Status.CANCELLED, Status.REFUNDED),
        (Status.REFUNDED, Status.CONFIRMED),
        (Status.REFUNDED, Status.CANCELLED),
        # A no-op "transition" to the same status is not a transition.
        (Status.CONFIRMED, Status.CONFIRMED),
    ],
)
def test_every_illegal_transition_is_rejected(from_status, to_status):
    assert is_legal_order_status_transition(from_status, to_status) is False
