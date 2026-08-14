"""Tests for reporting_agent.py — a read-and-aggregate agent, not extract-and-persist.

Divergence from expense_agent/income_agent's test shape, worth noting explicitly:
there is no save_<x> to mock here at all — reporting_agent never writes a row, so
there's nothing analogous to `@patch("expense_agent.save_expense")` in this file.
Most coverage is plain parametrized function calls against `_resolve_deterministic`
(pure Python, no mocking needed) rather than mocked Claude responses, since Claude is
only a fallback path here, not the primary implementation.
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_types import AgentRequest
from reporting_agent import _resolve_deterministic, handle, resolve_date_range

_DATASET_PATH = (
    Path(__file__).parent.parent / "evals" / "datasets" / "parse_report_request.json"
)
_CASES = json.loads(_DATASET_PATH.read_text())

# All dataset cases except the ones deliberately designed to miss every deterministic
# rule (those are covered by TestFallsBackToClaudeWhenUnrecognized instead).
_CLAUDE_FALLBACK_CASE_IDS = {"explicit-range-two-months", "followup-anterior"}
_DETERMINISTIC_CASES = [c for c in _CASES if c["id"] not in _CLAUDE_FALLBACK_CASE_IDS]


class TestResolveDeterministic:
    @pytest.mark.parametrize(
        "case", _DETERMINISTIC_CASES, ids=[c["id"] for c in _DETERMINISTIC_CASES]
    )
    def test_matches_eval_dataset(self, case):
        now = datetime.fromisoformat(case["now"])
        result = _resolve_deterministic(case["input"], now)
        assert result == case["expected"]

    def test_returns_none_for_unrecognized_explicit_range(self):
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")
        result = _resolve_deterministic("entre el 3 de marzo y el 20 de abril", now)
        assert result is None

    def test_returns_none_for_referential_followup(self):
        # Regression test for the Phase 4 fix: before it, this fell through every rule
        # and silently returned the first-of-month default instead of signaling that
        # it needs conversation history — the same "silent wrong default" failure mode
        # Phase 3 was built to eliminate, showing up in a new shape.
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")
        result = _resolve_deterministic("¿y la anterior?", now)
        assert result is None

    def test_still_resolves_followup_shaped_but_self_sufficient_phrase(self):
        # "la semana pasada" is followup-shaped but self-sufficient — must keep
        # resolving deterministically regardless of the referential-word fix above.
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")
        result = _resolve_deterministic("¿y la semana pasada?", now)
        assert result == {"min_date": "2026-06-15", "max_date": "2026-06-21"}


class TestFallsBackToClaudeWhenUnrecognized:
    @patch("reporting_agent.client")
    def test_resolve_date_range_calls_claude_for_unrecognized_text(self, mock_client):
        block = MagicMock(
            type="tool_use",
            input={"min_date": "2026-03-03", "max_date": "2026-04-20"},
        )
        mock_client.messages.create.return_value = MagicMock(content=[block])
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")

        result = resolve_date_range("entre el 3 de marzo y el 20 de abril", now)

        assert result == {"min_date": "2026-03-03", "max_date": "2026-04-20"}
        call = mock_client.messages.create.call_args
        assert call.kwargs["tool_choice"] == {
            "type": "tool",
            "name": "record_date_range",
        }
        assert "2026-06-25" in call.kwargs["system"]

    @patch("reporting_agent.client")
    def test_resolve_date_range_skips_claude_for_recognized_text(self, mock_client):
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")

        result = resolve_date_range("este mes", now)

        assert result == {"min_date": "2026-06-01", "max_date": "2026-06-25"}
        mock_client.messages.create.assert_not_called()

    @patch("reporting_agent.client")
    def test_resolve_date_range_threads_conversation_into_messages(self, mock_client):
        block = MagicMock(
            type="tool_use",
            input={"min_date": "2026-06-15", "max_date": "2026-06-21"},
        )
        mock_client.messages.create.return_value = MagicMock(content=[block])
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")
        conversation = [
            {"role": "user", "content": "cuánto gasté esta semana"},
            {"role": "assistant", "content": "📊 Resumen 22 jun – 25 jun 2026: ..."},
        ]

        result = resolve_date_range("¿y la anterior?", now, conversation)

        assert result == {"min_date": "2026-06-15", "max_date": "2026-06-21"}
        call = mock_client.messages.create.call_args
        messages = call.kwargs["messages"]
        # Prior turns first, current query last — real multi-turn history, not folded
        # into the system prompt.
        assert messages[:2] == conversation
        assert messages[2] == {"role": "user", "content": "¿y la anterior?"}

    @patch("reporting_agent.client")
    def test_resolve_date_range_defaults_conversation_to_empty(self, mock_client):
        # No conversation arg at all — existing Phase 3 call sites must keep working.
        block = MagicMock(
            type="tool_use",
            input={"min_date": "2026-03-03", "max_date": "2026-04-20"},
        )
        mock_client.messages.create.return_value = MagicMock(content=[block])
        now = datetime.fromisoformat("2026-06-25T10:00:00-05:00")

        resolve_date_range("entre el 3 de marzo y el 20 de abril", now)

        call = mock_client.messages.create.call_args
        assert call.kwargs["messages"] == [
            {"role": "user", "content": "entre el 3 de marzo y el 20 de abril"}
        ]


class TestHandle:
    @patch("reporting_agent.get_expenses")
    def test_queries_and_formats_report(self, mock_get_expenses):
        mock_get_expenses.return_value = [
            {"amount": 32000, "currency": "COP", "category": "comida"},
            {"amount": 14000, "currency": "COP", "category": "transporte"},
        ]
        request = AgentRequest(
            user_id=7,
            message_id=1,
            phone="+573001234567",
            text="este mes",
            media=[],
            message_type="text",
            now="2026-06-25T10:00:00-05:00",
            conversation=[],
        )

        response = handle(request)

        mock_get_expenses.assert_called_once_with(7, "2026-06-01", "2026-06-25")
        assert response.ok
        assert "comida" in response.reply_text
        assert "transporte" in response.reply_text
        # data holds the query results used to build the report — not a persisted row.
        assert response.data == mock_get_expenses.return_value
        assert response.reply_attachment is None

    @patch("reporting_agent.get_expenses")
    @patch("reporting_agent.client")
    def test_threads_request_conversation_into_resolve_date_range(
        self, mock_client, mock_get_expenses
    ):
        block = MagicMock(
            type="tool_use",
            input={"min_date": "2026-06-15", "max_date": "2026-06-21"},
        )
        mock_client.messages.create.return_value = MagicMock(content=[block])
        mock_get_expenses.return_value = []
        conversation = [
            {"role": "user", "content": "cuánto gasté esta semana"},
            {"role": "assistant", "content": "📊 Resumen 22 jun – 25 jun 2026: ..."},
        ]
        request = AgentRequest(
            user_id=7,
            message_id=1,
            phone="+573001234567",
            text="¿y la anterior?",
            media=[],
            message_type="text",
            now="2026-06-25T10:00:00-05:00",
            conversation=conversation,
        )

        handle(request)

        mock_get_expenses.assert_called_once_with(7, "2026-06-15", "2026-06-21")
        messages = mock_client.messages.create.call_args.kwargs["messages"]
        assert messages[:2] == conversation

    @patch("reporting_agent.get_expenses")
    def test_no_expenses_in_range(self, mock_get_expenses):
        mock_get_expenses.return_value = []
        request = AgentRequest(
            user_id=7,
            message_id=1,
            phone="+573001234567",
            text="la semana pasada",
            media=[],
            message_type="text",
            now="2026-06-25T10:00:00-05:00",
            conversation=[],
        )

        response = handle(request)

        assert response.ok
        assert "Sin gastos registrados" in response.reply_text
        assert response.data == []
