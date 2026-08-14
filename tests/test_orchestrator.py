from unittest.mock import patch

import pytest

import orchestrator
from agent_types import AgentRequest


def _request() -> AgentRequest:
    return AgentRequest(
        user_id=1,
        message_id=1,
        phone="+573001234567",
        text="almuerzo 32000",
        media=[],
        message_type="text",
        now="2026-08-14T10:00:00-05:00",
        categories=["comida"],
        conversation=[],
    )


@patch("orchestrator.expense_agent")
def test_handle_message_dispatches_to_expense_agent(mock_expense_agent):
    mock_expense_agent.handle.return_value = "sentinel-response"
    request = _request()

    result = orchestrator.handle_message(request)

    mock_expense_agent.handle.assert_called_once_with(request)
    assert result == "sentinel-response"
