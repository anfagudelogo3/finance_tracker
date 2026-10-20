from unittest.mock import patch

import pytest

from finance_tracker.routing import orchestrator
from finance_tracker.routing.fast_paths import is_income_request
from finance_tracker.config import MSG_ERROR
from finance_tracker.core.agent_types import AgentRequest, AgentResponse


def _request(text: str = "almuerzo 32000") -> AgentRequest:
    return AgentRequest(
        user_id=1,
        message_id=1,
        phone="+573001234567",
        text=text,
        media=[],
        message_type="text",
        now="2026-08-14T10:00:00-05:00",
        conversation=[],
    )


class TestIsIncomeRequest:
    @pytest.mark.parametrize(
        "text",
        [
            "me pagaron 2000000",
            "me depositaron mi salario",
            "recibí mi quincena",
            "cobré mi sueldo",
            "pago de nomina 3000000",
            "i got paid 500 dollars",
            "my salary came in",
        ],
    )
    def test_matches_income_phrases(self, text):
        assert is_income_request(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "almuerzo 32000",
            "pagué el arriendo",
            "pague la factura de servicios",
            "mi pago del arriendo quedo pendiente",
            "uber 14000",
            "compre mercado por 50000",
        ],
    )
    def test_does_not_match_expense_phrases(self, text):
        assert is_income_request(text) is False


class TestHandleMessage:
    @patch("finance_tracker.routing.orchestrator.reporting")
    @patch("finance_tracker.routing.orchestrator.incomes")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_dispatches_expense_text_to_expense_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_expense_agent.handle.return_value = AgentResponse(ok=True, reply_text="expense-response")
        request = _request("almuerzo 32000")

        result = orchestrator.handle_message(request)

        mock_expense_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        mock_reporting_agent.handle.assert_not_called()
        assert result.reply_text == "expense-response"

    @patch("finance_tracker.routing.orchestrator.reporting")
    @patch("finance_tracker.routing.orchestrator.incomes")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_dispatches_income_text_to_income_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_income_agent.handle.return_value = AgentResponse(ok=True, reply_text="income-response")
        request = _request("me pagaron 2000000")

        result = orchestrator.handle_message(request)

        mock_income_agent.handle.assert_called_once_with(request)
        mock_expense_agent.handle.assert_not_called()
        mock_reporting_agent.handle.assert_not_called()
        assert result.reply_text == "income-response"

    @patch("finance_tracker.routing.orchestrator.reporting")
    @patch("finance_tracker.routing.orchestrator.incomes")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_dispatches_report_text_to_reporting_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_reporting_agent.handle.return_value = AgentResponse(ok=True, reply_text="report-response")
        request = _request("cuánto gasté este mes")

        result = orchestrator.handle_message(request)

        mock_reporting_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        mock_expense_agent.handle.assert_not_called()
        assert result.reply_text == "report-response"

    @patch("finance_tracker.routing.orchestrator.reporting")
    @patch("finance_tracker.routing.orchestrator.incomes")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_report_takes_priority_over_income_phrasing(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        # "resumen de mis ingresos" is a report about income, not an instruction to
        # log new income — report must be checked before income.
        mock_reporting_agent.handle.return_value = AgentResponse(ok=True, reply_text="report-response")
        request = _request("resumen de mis ingresos")

        result = orchestrator.handle_message(request)

        mock_reporting_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        assert result.reply_text == "report-response"


class TestRouterFallback:
    @patch("finance_tracker.routing.orchestrator.router_agent")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_unclear_message_goes_to_router(self, mock_expense_agent, mock_router):
        mock_router.handle.return_value = AgentResponse(ok=True, reply_text="router-response")
        request = _request("nequi 50000")

        result = orchestrator.handle_message(request)

        mock_router.handle.assert_called_once_with(request)
        mock_expense_agent.handle.assert_not_called()
        assert result.reply_text == "router-response"

    @patch("finance_tracker.routing.orchestrator.router_agent")
    @patch("finance_tracker.routing.orchestrator.expenses")
    def test_business_profile_skips_fast_paths(self, mock_expense_agent, mock_router):
        request = _request("almuerzo 32000")
        request.profile = {
            "type": "business",
            "business": {"industry": "restaurante", "description": "Vende almuerzos"},
        }
        mock_router.handle.return_value = AgentResponse(ok=True, reply_text="router")

        orchestrator.handle_message(request)

        mock_router.handle.assert_called_once_with(request)
        mock_expense_agent.handle.assert_not_called()


class TestSettlesMoves:
    @patch("finance_tracker.routing.orchestrator.records")
    @patch("finance_tracker.routing.orchestrator.router_agent")
    def test_a_half_done_move_is_reported_after_the_agents(self, mock_router, mock_records):
        mock_router.handle.return_value = AgentResponse(ok=True, reply_text="")
        mock_records.settle_moves.return_value = ["⚠️ No pude pasar …"]

        result = orchestrator.handle_message(_request("este no es un gasto es un ingreso"))

        mock_records.start_message.assert_called_once()
        mock_records.settle_moves.assert_called_once_with(1, 1)
        assert result.reply_text == "⚠️ No pude pasar …"

    @patch("finance_tracker.routing.orchestrator.records")
    @patch("finance_tracker.routing.orchestrator.router_agent")
    def test_never_an_empty_reply(self, mock_router, mock_records):
        mock_router.handle.return_value = AgentResponse(ok=True, reply_text="")
        mock_records.settle_moves.return_value = []

        result = orchestrator.handle_message(_request("hola"))

        assert result.reply_text == MSG_ERROR
