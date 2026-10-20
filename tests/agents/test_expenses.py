from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.core.agent_types import AgentRequest
from finance_tracker.config import MSG_EMPTY_EXPENSE
from finance_tracker.agents.expenses import handle, _build_system_prompt

_DEFAULT_CATEGORIES = ["comida", "transporte", "mercado", "entretenimiento", "otro"]


def _tool_use_response(expenses: list[dict]) -> MagicMock:
    block = MagicMock(type="tool_use", input={"expenses": expenses})
    return MagicMock(content=[block])


def _request(text: str) -> AgentRequest:
    return AgentRequest(
        user_id=7,
        message_id=1,
        phone="+573001234567",
        text=text,
        media=[],
        message_type="text",
        now="2026-08-14T10:00:00-05:00",
        conversation=[],
    )


class TestHandle:
    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_parses_simple_expense(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_expense.return_value = 101
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 32000,
                    "currency": "COP",
                    "category": "comida",
                    "payment_method": None,
                    "merchant": None,
                    "description": "almuerzo",
                }
            ]
        )

        response = handle(_request("almuerzo 32000"))

        assert response.ok
        assert len(response.data) == 1
        assert response.data[0]["amount"] == 32000
        assert response.data[0]["category"] == "comida"
        assert response.data[0]["expense_id"] == 101
        assert "date" in response.data[0]
        assert "confidence" in response.data[0]
        mock_save_expense.assert_called_once()

    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_parses_expense_with_payment_method(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_expense.return_value = 102
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 120000,
                    "currency": "COP",
                    "category": "mercado",
                    "payment_method": "tarjeta",
                    "merchant": None,
                    "description": "mercado con tarjeta",
                }
            ]
        )

        response = handle(_request("mercado 120000 tarjeta"))

        assert response.data[0]["amount"] == 120000
        assert response.data[0]["payment_method"] == "tarjeta"

    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_parses_multiple_expenses(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_expense.side_effect = [201, 202]
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 20000,
                    "currency": "COP",
                    "category": "comida",
                    "payment_method": None,
                    "merchant": None,
                    "description": "almuerzo",
                },
                {
                    "amount": 40000,
                    "currency": "COP",
                    "category": "entretenimiento",
                    "payment_method": None,
                    "merchant": None,
                    "description": "cine",
                },
            ]
        )

        response = handle(_request("almuerzo 20 luca y cine 40 mil"))

        assert len(response.data) == 2
        assert response.data[0]["amount"] == 20000
        assert response.data[1]["amount"] == 40000
        assert mock_save_expense.call_count == 2

    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_no_expenses_returns_empty_fallback(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_client.messages.create.return_value = _tool_use_response([])

        response = handle(_request("gracias"))

        assert response.ok
        assert response.data is None
        assert response.reply_text == MSG_EMPTY_EXPENSE
        mock_save_expense.assert_not_called()

    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_injects_user_categories_into_prompt_and_forces_tool(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = ["mascota", "comida"]
        mock_save_expense.return_value = 301
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 5000,
                    "currency": "COP",
                    "category": "mascota",
                    "payment_method": None,
                    "merchant": None,
                    "description": "croquetas",
                }
            ]
        )

        handle(_request("croquetas 5000"))

        mock_get_categories.assert_called_once_with(7, kind="expense")
        call = mock_client.messages.create.call_args
        assert "mascota" in call.kwargs["system"]
        tool = call.kwargs["tools"][0]
        assert tool["name"] == "record_expenses"
        category_schema = tool["input_schema"]["properties"]["expenses"]["items"][
            "properties"
        ]["category"]
        assert "mascota" in category_schema["enum"]
        assert call.kwargs["tool_choice"] == {"type": "tool", "name": "record_expenses"}


class TestBuildSystemPrompt:
    def test_uses_provided_categories(self):
        prompt = _build_system_prompt(["mascota", "viajes"])
        assert "mascota, viajes" in prompt


class TestMessageDateAndCurrency:
    def test_record_date_is_the_messages_bogota_date(self):
        """A message held during onboarding is saved with its own date, not today's."""
        from dataclasses import replace

        from finance_tracker.agents import expenses as agent

        request = replace(_request("almuerzo 20000"), now="2026-10-19T23:30:00-05:00")
        with patch.object(agent, "get_user_categories", return_value=["otro"]), patch.object(
            agent, "save_expense", return_value=1
        ), patch.object(agent, "_extract", return_value=[{"amount": 20000, "currency": "COP",
                "category": "otro", "description": "x", "payment_method": None, "merchant": None}]):
            response = agent.handle(request)

        assert response.data[0]["date"] == "2026-10-19"

    def test_default_currency_reaches_the_prompt(self):
        prompt = _build_system_prompt(["otro"], "USD")
        assert 'assume "USD"' in prompt
        assert 'assume "COP"' in _build_system_prompt(["otro"])


class TestRecordDates:
    """ "ayer gasté 10 mil": the record date comes from the message; created_at (the
    ingest time) is the database's and isn't touched here."""

    def _handle(self, records, now="2026-10-22T09:00:00-05:00"):
        from dataclasses import replace

        from finance_tracker.agents import expenses as agent

        request = replace(_request("x"), now=now)
        with patch.object(agent, "get_user_categories", return_value=["otro"]), patch.object(
            agent, "save_expense", return_value=1
        ) as save, patch.object(agent, "_extract", return_value=records) as extract:
            response = agent.handle(request)
        return response, save, extract

    def _record(self, **overrides):
        return {"amount": 10000, "currency": "COP", "category": "otro", "description": "almuerzo",
                 "payment_method": None, "merchant": None, "date": None, **overrides}

    def test_mentioned_date_is_used(self):
        response, _, extract = self._handle([self._record(date="2026-10-21")])
        assert response.data[0]["date"] == "2026-10-21"
        # the model is told the message's date, so "ayer" is relative to it
        assert extract.call_args.args[3].isoformat() == "2026-10-22"

    def test_future_date_is_not_recorded(self):
        response, save, _ = self._handle([self._record(amount=1000000, date="2026-10-23")])
        save.assert_not_called()
        assert response.data is None
        assert response.reply_text.startswith("📅 No registré COP 1.000.000 (almuerzo)")
        assert "2026-10-23" in response.reply_text

    def test_mixed_saves_the_past_and_explains_the_future(self):
        response, save, _ = self._handle([
            self._record(date="2026-10-21"),
            self._record(amount=50000, date="2026-10-30"),
        ])
        assert save.call_count == 1
        confirmation, note = response.reply_text.split("\n\n")
        assert "2026-10-21" in confirmation and note.startswith("📅")

    def test_today_line_is_in_the_prompt(self):
        from datetime import date

        prompt = _build_system_prompt(["otro"], "COP", date(2026, 10, 22))
        assert "Today is jueves 2026-10-22" in prompt
