from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.core.agent_types import AgentRequest
from finance_tracker.config import MSG_EMPTY_EXPENSE
from finance_tracker.agents.expenses import handle, _build_system_prompt

_DEFAULT_CATEGORIES = ["comida", "transporte", "mercado", "entretenimiento", "otro"]


def _tool_use_response(expenses: list[dict]) -> MagicMock:
    """Claude calling record_expenses."""
    block = MagicMock(type="tool_use", id="tu_1", input={"expenses": expenses})
    block.name = "record_expenses"
    return MagicMock(stop_reason="tool_use", content=[block])


def _end_turn() -> MagicMock:
    """Claude ending its turn after the tool results: the loop stops only here."""
    return MagicMock(stop_reason="end_turn", content=[])


class _records_from_claude:
    """Context manager: make the mocked Claude client call record_expenses with `records`."""

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
    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    def test_parses_simple_expense(
        self, mock_client, mock_get_categories, mock_save_expense
    ):
        mock_get_categories.return_value = _DEFAULT_CATEGORIES
        mock_save_expense.return_value = 101
        mock_client.messages.create.side_effect = [_tool_use_response(
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
        ), _end_turn()]

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
        mock_client.messages.create.side_effect = [_tool_use_response(
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
        ), _end_turn()]

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
        mock_client.messages.create.side_effect = [_tool_use_response(
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
        ), _end_turn()]

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
        mock_client.messages.create.side_effect = [_tool_use_response([]), _end_turn()]

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
        mock_client.messages.create.side_effect = [_tool_use_response(
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
        ), _end_turn()]

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
        assert call.kwargs["tool_choice"] == {"type": "auto"}  # Claude decides when it's done


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

        from finance_tracker.agents import expenses as agent

        request = replace(_request("x"), now=now)
        with patch.object(agent, "get_user_categories", return_value=["otro"]), patch.object(
            agent, "save_expense", return_value=1
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


class TestCorrectionFlow:
    def test_find_then_update_a_swipe_replied_record(self):
        """find → update → end_turn, reply written in code, no new record."""
        from dataclasses import replace

        from finance_tracker.agents import expenses as agent

        def call(name, tool_input, block_id):
            block = MagicMock(type="tool_use", id=block_id, input=tool_input)
            block.name = name
            return MagicMock(stop_reason="tool_use", content=[block])

        replied = {"role": "assistant", "text": "💸 …", "date": None, "source": "database",
                   "records": [{"kind": "expense", "id": 12, "amount": 25000.0, "currency": "COP",
                                "category": "comida", "date": "2026-10-20", "description": "almuerzo"}]}
        request = replace(_request("eran 15000"), replied_to=replied)
        record = {"id": 12, "amount": 25000, "currency": "COP", "category_id": 3,
                  "category": "comida", "date": "2026-10-20", "description": "almuerzo",
                  "payment_method": None, "merchant": None, "created_at": None}
        with patch.object(agent, "client") as fake_client, patch.object(
            agent, "get_user_categories", return_value=["comida", "otro"]
        ), patch.object(agent, "save_expense") as save, patch(
            "finance_tracker.agents.records.database"
        ) as db:
            fake_client.messages.create.side_effect = [
                call("find_records", {"text": "comida", "amount": None, "date_from": None,
                                      "date_to": None, "last": None}, "tu_1"),
                call("update_record", {"record_id": 12, "changes": {"amount": 15000}}, "tu_2"),
                _end_turn(),
            ]
            db.get_recent_records.return_value = [record]
            db.get_record.return_value = record
            db.update_record.return_value = {"before": {**record, "amount": 25000.0},
                                             "after": {**record, "amount": 15000.0}}

            response = agent.handle(request)

        assert fake_client.messages.create.call_count == 3
        save.assert_not_called()
        db.update_record.assert_called_once_with(7, "expense", 12, {"amount": 15000}, 1, "expense_agent")
        assert response.reply_text.startswith("✏️ Gasto actualizado: monto COP 25.000 → COP 15.000")
        # the quoted record reached the model
        content = fake_client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
        assert "<replied_to" in content and "<message>eran 15000</message>" in content

    def test_safeguard_with_nothing_done_is_an_error(self):
        from finance_tracker.agents import expenses as agent
        from finance_tracker.config import MSG_ERROR

        with patch.object(agent, "client") as fake_client, patch.object(
            agent, "get_user_categories", return_value=["otro"]
        ):
            fake_client.messages.create.return_value = MagicMock(stop_reason="max_tokens", content=[])
            response = agent.handle(_request("x"))

        assert not response.ok and response.reply_text == MSG_ERROR
