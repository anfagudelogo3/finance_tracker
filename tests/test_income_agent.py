from unittest.mock import MagicMock, patch

import pytest

from agent_types import AgentRequest
from config import MSG_EMPTY_INCOME
from income_agent import handle, _build_system_prompt

_DEFAULT_CATEGORIES = [
    "salario",
    "freelance",
    "negocio",
    "inversion",
    "reembolso",
    "regalo",
    "otro",
]


def _tool_use_response(incomes: list[dict]) -> MagicMock:
    block = MagicMock(type="tool_use", input={"incomes": incomes})
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
    @patch("income_agent.save_income")
    @patch("income_agent.get_user_categories")
    @patch("income_agent.client")
    def test_parses_simple_income(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.return_value = 101
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 2000000,
                    "currency": "COP",
                    "category": "salario",
                    "description": "pago de salario",
                }
            ]
        )

        response = handle(_request("me pagaron 2000000 de salario"))

        assert response.ok
        assert len(response.data) == 1
        assert response.data[0]["amount"] == 2000000
        assert response.data[0]["category"] == "salario"
        assert response.data[0]["income_id"] == 101
        assert response.data[0]["source"] == "manual"
        assert "date" in response.data[0]
        assert "confidence" in response.data[0]
        mock_save_income.assert_called_once()

    @patch("income_agent.save_income")
    @patch("income_agent.get_user_categories")
    @patch("income_agent.client")
    def test_parses_income_with_non_default_category_and_currency(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.return_value = 102
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 500,
                    "currency": "USD",
                    "category": "freelance",
                    "description": "pago de cliente",
                }
            ]
        )

        response = handle(_request("me pagaron 500 dolares de un cliente"))

        assert response.data[0]["amount"] == 500
        assert response.data[0]["currency"] == "USD"
        assert response.data[0]["category"] == "freelance"

    @patch("income_agent.save_income")
    @patch("income_agent.get_user_categories")
    @patch("income_agent.client")
    def test_parses_multiple_incomes(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.side_effect = [201, 202]
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 3000000,
                    "currency": "COP",
                    "category": "salario",
                    "description": "salario",
                },
                {
                    "amount": 200000,
                    "currency": "COP",
                    "category": "regalo",
                    "description": "regalo",
                },
            ]
        )

        response = handle(
            _request(
                "me depositaron mi salario de 3000000 y tambien 200000 de un regalo"
            )
        )

        assert len(response.data) == 2
        assert response.data[0]["amount"] == 3000000
        assert response.data[1]["amount"] == 200000
        assert mock_save_income.call_count == 2

    @patch("income_agent.save_income")
    @patch("income_agent.get_user_categories")
    @patch("income_agent.client")
    def test_no_incomes_returns_empty_fallback(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_client.messages.create.return_value = _tool_use_response([])

        response = handle(_request("gracias"))

        assert response.ok
        assert response.data is None
        assert response.reply_text == MSG_EMPTY_INCOME
        mock_save_income.assert_not_called()

    @patch("income_agent.save_income")
    @patch("income_agent.get_user_categories")
    @patch("income_agent.client")
    def test_injects_user_categories_into_prompt_and_forces_tool(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = ["consultoria", "salario"]
        mock_save_income.return_value = 301
        mock_client.messages.create.return_value = _tool_use_response(
            [
                {
                    "amount": 800000,
                    "currency": "COP",
                    "category": "consultoria",
                    "description": "consultoria",
                }
            ]
        )

        handle(_request("me pagaron 800000 por consultoria"))

        mock_get_categories.assert_called_once_with(7, kind="income")
        call = mock_client.messages.create.call_args
        assert "consultoria" in call.kwargs["system"]
        tool = call.kwargs["tools"][0]
        assert tool["name"] == "record_incomes"
        category_schema = tool["input_schema"]["properties"]["incomes"]["items"][
            "properties"
        ]["category"]
        assert "consultoria" in category_schema["enum"]
        assert call.kwargs["tool_choice"] == {"type": "tool", "name": "record_incomes"}


class TestBuildSystemPrompt:
    def test_uses_provided_categories(self):
        prompt = _build_system_prompt(["salario", "freelance"])
        assert "salario, freelance" in prompt
