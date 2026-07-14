from unittest.mock import patch

import pytest

from apps.distributors.tasks import consume_didit_result_task


@pytest.mark.django_db
@patch("apps.distributors.tasks.consume_didit_result")
def test_task_calls_consume_didit_result_with_the_session_id(mock_consume):
    consume_didit_result_task("sess-abc")

    mock_consume.assert_called_once_with("sess-abc")
