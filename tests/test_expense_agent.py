from unittest.mock import MagicMock, patch

import pytest

from agent_types import AgentRequest
from config import MSG_EMPTY_EXPENSE
from expense_agent import handle, _build_system_prompt

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
    @patch("expense_agent.save_expense")
    @patch("expense_agent.get_user_categories")
    @patch("expense_agent.client")
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

    @patch("expense_agent.save_expense")
    @patch("expense_agent.get_user_categories")
    @patch("expense_agent.client")
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

    @patch("expense_agent.save_expense")
    @patch("expense_agent.get_user_categories")
    @patch("expense_agent.client")
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

    @patch("expense_agent.save_expense")
    @patch("expense_agent.get_user_categories")
    @patch("expense_agent.client")
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

    @patch("expense_agent.save_expense")
    @patch("expense_agent.get_user_categories")
    @patch("expense_agent.client")
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
