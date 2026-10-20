from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.core.agent_types import AgentRequest
from finance_tracker.config import MSG_EMPTY_INCOME
from finance_tracker.agents.incomes import handle, _build_system_prompt

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
    """Claude calling record_incomes."""
    block = MagicMock(type="tool_use", id="tu_1", input={"incomes": incomes})
    block.name = "record_incomes"
    return MagicMock(stop_reason="tool_use", content=[block])


def _end_turn() -> MagicMock:
    """Claude ending its turn after the tool results: the loop stops only here."""
    return MagicMock(stop_reason="end_turn", content=[])


class _records_from_claude:
    """Context manager: make the mocked Claude client call record_incomes with `records`."""

    def __init__(self, client, records):
        client.messages.create.side_effect = [_tool_use_response(records), _end_turn()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    def test_parses_simple_income(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.return_value = 101
        mock_client.messages.create.side_effect = [_tool_use_response(
            [
                {
                    "amount": 2000000,
                    "currency": "COP",
                    "category": "salario",
                    "description": "pago de salario",
                }
            ]
        ), _end_turn()]

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

    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    def test_parses_income_with_non_default_category_and_currency(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.return_value = 102
        mock_client.messages.create.side_effect = [_tool_use_response(
            [
                {
                    "amount": 500,
                    "currency": "USD",
                    "category": "freelance",
                    "description": "pago de cliente",
                }
            ]
        ), _end_turn()]

        response = handle(_request("me pagaron 500 dolares de un cliente"))

        assert response.data[0]["amount"] == 500
        assert response.data[0]["currency"] == "USD"
        assert response.data[0]["category"] == "freelance"

    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    def test_parses_multiple_incomes(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_income.side_effect = [201, 202]
        mock_client.messages.create.side_effect = [_tool_use_response(
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
        ), _end_turn()]

        response = handle(
            _request(
                "me depositaron mi salario de 3000000 y tambien 200000 de un regalo"
            )
        )

        assert len(response.data) == 2
        assert response.data[0]["amount"] == 3000000
        assert response.data[1]["amount"] == 200000
        assert mock_save_income.call_count == 2

    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    def test_no_incomes_returns_empty_fallback(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_client.messages.create.side_effect = [_tool_use_response([]), _end_turn()]

        response = handle(_request("gracias"))

        assert response.ok
        assert response.data is None
        assert response.reply_text == MSG_EMPTY_INCOME
        mock_save_income.assert_not_called()

    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    def test_injects_user_categories_into_prompt_and_forces_tool(
        self, mock_client, mock_get_categories, mock_save_income
    ):
        mock_get_categories.return_value = ["consultoria", "salario"]
        mock_save_income.return_value = 301
        mock_client.messages.create.side_effect = [_tool_use_response(
            [
                {
                    "amount": 800000,
                    "currency": "COP",
                    "category": "consultoria",
                    "description": "consultoria",
                }
            ]
        ), _end_turn()]

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
        assert call.kwargs["tool_choice"] == {"type": "auto"}  # Claude decides when it's done


class TestBuildSystemPrompt:
    def test_uses_provided_categories(self):
        prompt = _build_system_prompt(["salario", "freelance"])
        assert "salario, freelance" in prompt


class TestMessageDateAndCurrency:
    def test_record_date_is_the_messages_bogota_date(self):
        """A message held during onboarding is saved with its own date, not today's."""
        from dataclasses import replace

        from finance_tracker.agents import incomes as agent

        request = replace(_request("salario 20000"), now="2026-10-19T23:30:00-05:00")
        with patch.object(agent, "get_user_categories", return_value=["otro"]), patch.object(
            agent, "save_income", return_value=1
        ), patch.object(agent, "client") as fake_client, _records_from_claude(fake_client, [{"amount": 20000, "currency": "COP",
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

        from finance_tracker.agents import incomes as agent

        request = replace(_request("x"), now=now)
        with patch.object(agent, "get_user_categories", return_value=["otro"]), patch.object(
            agent, "save_income", return_value=1
        ) as save, patch.object(agent, "client") as fake_client, _records_from_claude(fake_client, records):
            response = agent.handle(request)
        return response, save, fake_client

    def _record(self, **overrides):
        return {"amount": 10000, "currency": "COP", "category": "otro", "description": "almuerzo",
                 "payment_method": None, "merchant": None, "date": None, **overrides}

    def test_mentioned_date_is_used(self):
        response, _, fake_client = self._handle([self._record(date="2026-10-21")])
        assert response.data[0]["date"] == "2026-10-21"
        # the model is told the message's date, so "ayer" is relative to it
        system = fake_client.messages.create.call_args.kwargs["system"]
        assert "Today is jueves 2026-10-22" in system

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
