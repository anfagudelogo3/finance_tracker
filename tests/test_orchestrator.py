from unittest.mock import patch

import pytest

import orchestrator
from agent_types import AgentRequest


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
        assert orchestrator.is_income_request(text) is True

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
        assert orchestrator.is_income_request(text) is False


class TestHandleMessage:
    @patch("orchestrator.reporting_agent")
    @patch("orchestrator.income_agent")
    @patch("orchestrator.expense_agent")
    def test_dispatches_expense_text_to_expense_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_expense_agent.handle.return_value = "expense-response"
        request = _request("almuerzo 32000")

        result = orchestrator.handle_message(request)

        mock_expense_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        mock_reporting_agent.handle.assert_not_called()
        assert result == "expense-response"

    @patch("orchestrator.reporting_agent")
    @patch("orchestrator.income_agent")
    @patch("orchestrator.expense_agent")
    def test_dispatches_income_text_to_income_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_income_agent.handle.return_value = "income-response"
        request = _request("me pagaron 2000000")

        result = orchestrator.handle_message(request)

        mock_income_agent.handle.assert_called_once_with(request)
        mock_expense_agent.handle.assert_not_called()
        mock_reporting_agent.handle.assert_not_called()
        assert result == "income-response"

    @patch("orchestrator.reporting_agent")
    @patch("orchestrator.income_agent")
    @patch("orchestrator.expense_agent")
    def test_dispatches_report_text_to_reporting_agent(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        mock_reporting_agent.handle.return_value = "report-response"
        request = _request("cuánto gasté este mes")

        result = orchestrator.handle_message(request)

        mock_reporting_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        mock_expense_agent.handle.assert_not_called()
        assert result == "report-response"

    @patch("orchestrator.reporting_agent")
    @patch("orchestrator.income_agent")
    @patch("orchestrator.expense_agent")
    def test_report_takes_priority_over_income_phrasing(
        self, mock_expense_agent, mock_income_agent, mock_reporting_agent
    ):
        # "resumen de mis ingresos" is a report about income, not an instruction to
        # log new income — report must be checked before income.
        mock_reporting_agent.handle.return_value = "report-response"
        request = _request("resumen de mis ingresos")

        result = orchestrator.handle_message(request)

        mock_reporting_agent.handle.assert_called_once_with(request)
        mock_income_agent.handle.assert_not_called()
        assert result == "report-response"
