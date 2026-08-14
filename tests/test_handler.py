from urllib.parse import urlencode
from unittest.mock import MagicMock, patch

import pytest

from handler import _handle_message
from config import MSG_ERROR


def _make_event(body: str = "almuerzo 32000", message_id: str = "SM123") -> dict:
    form = {
        "From": "whatsapp:+573001234567",
        "Body": body,
        "MessageSid": message_id,
        "NumMedia": "0",
    }
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"x-twilio-signature": "sig"},
        "body": urlencode(form),
        "isBase64Encoded": False,
    }


def _tool_use_response(expenses: list[dict]) -> MagicMock:
    block = MagicMock(type="tool_use", input={"expenses": expenses})
    return MagicMock(content=[block])


class TestExpenseTextPath:
    @patch("expense_agent.save_expense")
    @patch("expense_agent.client")
    @patch("handler.send_message")
    @patch("handler.save_turn")
    @patch("handler.get_user_categories")
    @patch("handler.save_message")
    @patch("handler.get_or_create_user")
    @patch("handler.verify_signature")
    def test_text_expense_routes_through_orchestrator_and_persists(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_get_categories,
        mock_save_turn,
        mock_send_message,
        mock_claude_client,
        mock_save_expense,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_get_categories.return_value = ["comida", "otro"]
        mock_send_message.return_value = "SIDxxx"
        mock_save_expense.return_value = 101
        mock_claude_client.messages.create.return_value = _tool_use_response(
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

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_save_expense.assert_called_once()
        mock_send_message.assert_called_once()
        assert mock_save_turn.call_count == 2  # inbound + outbound


class TestExcelAndReportBypassOrchestrator:
    @patch("handler.orchestrator")
    @patch("handler.send_document")
    @patch("handler.upload_and_sign")
    @patch("handler.create_excel_bytes")
    @patch("handler.get_expenses")
    @patch("handler.parse_report_request")
    @patch("handler.save_turn")
    @patch("handler.get_user_categories")
    @patch("handler.save_message")
    @patch("handler.get_or_create_user")
    @patch("handler.verify_signature")
    def test_excel_request_does_not_reach_orchestrator(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_get_categories,
        mock_save_turn,
        mock_parse_report,
        mock_get_expenses,
        mock_create_excel,
        mock_upload,
        mock_send_document,
        mock_orchestrator,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_get_categories.return_value = ["comida"]
        mock_parse_report.return_value = {
            "min_date": "2026-08-01",
            "max_date": "2026-08-14",
        }
        mock_get_expenses.return_value = []
        mock_create_excel.return_value = b"xlsx-bytes"
        mock_upload.return_value = "https://example.com/presigned"
        mock_send_document.return_value = "SIDxxx"

        result = _handle_message(_make_event("exportame el excel de mis gastos"))

        assert result["statusCode"] == 200
        mock_orchestrator.handle_message.assert_not_called()

    @patch("handler.orchestrator")
    @patch("handler.send_message")
    @patch("handler.get_expenses")
    @patch("handler.parse_report_request")
    @patch("handler.save_turn")
    @patch("handler.get_user_categories")
    @patch("handler.save_message")
    @patch("handler.get_or_create_user")
    @patch("handler.verify_signature")
    def test_report_request_does_not_reach_orchestrator(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_get_categories,
        mock_save_turn,
        mock_parse_report,
        mock_get_expenses,
        mock_send_message,
        mock_orchestrator,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_get_categories.return_value = ["comida"]
        mock_parse_report.return_value = {
            "min_date": "2026-08-01",
            "max_date": "2026-08-14",
        }
        mock_get_expenses.return_value = []
        mock_send_message.return_value = "SIDxxx"

        result = _handle_message(_make_event("resumen de mis gastos"))

        assert result["statusCode"] == 200
        mock_orchestrator.handle_message.assert_not_called()


class TestErrorContract:
    @patch("expense_agent.client")
    @patch("handler.send_message")
    @patch("handler.save_turn")
    @patch("handler.get_user_categories")
    @patch("handler.save_message")
    @patch("handler.get_or_create_user")
    @patch("handler.verify_signature")
    def test_unhandled_exception_sends_msg_error_and_returns_200(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_get_categories,
        mock_save_turn,
        mock_send_message,
        mock_claude_client,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_get_categories.return_value = ["comida"]
        mock_claude_client.messages.create.side_effect = RuntimeError("boom")

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_send_message.assert_called_once_with("+573001234567", MSG_ERROR)


class TestDuplicateMessage:
    @patch("handler.orchestrator")
    @patch("handler.save_message")
    @patch("handler.get_or_create_user")
    @patch("handler.verify_signature")
    def test_duplicate_message_short_circuits_before_orchestrator(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_orchestrator,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = None  # ON CONFLICT DO NOTHING → duplicate

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_orchestrator.handle_message.assert_not_called()
