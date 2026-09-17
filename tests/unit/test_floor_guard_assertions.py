"""Task 64. Which removed test lines count as a removed assertion.

The floor guard's rule "no assertion quietly removed from a test" only
recognised `assert`, `assertRaises` and `pytest.raises`. Deleting a mock
check such as `mock_sms.assert_called_once()` or `task.delay.assert_not_called()`
went unnoticed, because the underscore after "assert" means `\\bassert\\b`
never matches, and so did every unittest check except assertRaises. This
codebase leans on mock checks heavily -- Task 62's receipt tests alone use
dozens -- so the gap was a real way to weaken a test without a trace.
"""

import pytest

from floor_guard import ASSERTION


@pytest.mark.parametrize(
    "line",
    [
        # Already recognised before Task 64 -- must stay recognised.
        "    assert order.status == Order.Status.CONFIRMED",
        "    with pytest.raises(SmsOutOfCredit):",
        "        self.assertRaises(ValueError, parse, '')",
        # Mock checks: the gap.
        "    mock_sms.assert_called_once()",
        "    task.delay.assert_called_once_with(order.pk)",
        "    task.delay.assert_not_called()",
        "    retry.assert_called_once_with(countdown=60)",
        "    mock_send.assert_any_call('+233241234567', 'Hello')",
        "    mock_post.assert_has_calls([call(1), call(2)])",
        "    handler.assert_awaited_once()",
        "    mock_log.assert_not_awaited()",
        # unittest-style checks other than assertRaises: also a gap.
        "        self.assertEqual(response.status_code, 200)",
        "        self.assertTrue(order.is_paid)",
        "        self.assertIn('Receipt', body)",
        "        self.assertContains(response, 'Order confirmed')",
        "        self.assertRedirects(response, '/login/')",
        # Failing a test outright is an assertion too.
        "        pytest.fail('should have raised')",
    ],
)
def test_counts_as_an_assertion(line):
    assert ASSERTION.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "    order = _make_order(email='')",
        "    response = client.post(reverse('distributors:login'))",
        "    mock_sms = patch('apps.notifications.otp.send_sms')",
        # A word merely containing "assert" is not an assertion.
        "    reassert_lock(key)",
        "    assertion_count = 3",
        "    RECEIPT_SEND_MAX_RETRIES = 3",
    ],
)
def test_ordinary_code_is_not_an_assertion(line):
    assert not ASSERTION.search(line)
