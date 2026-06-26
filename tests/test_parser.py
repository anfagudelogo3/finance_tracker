import json
from unittest.mock import MagicMock, patch

import pytest

from parser import parse_expense, _estimate_confidence, _build_system_prompt


class TestParseExpense:
    @patch("parser.client")
    def test_parses_simple_expense(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"expenses": [{
                "amount": 32000,
                "category": "comida",
                "currency": "COP",
                "payment_method": None,
                "merchant": None,
                "description": "almuerzo",
            }]})))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        result = parse_expense("almuerzo 32000")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["amount"] == 32000
        assert result[0]["category"] == "comida"
        assert "date" in result[0]
        assert "confidence" in result[0]

    @patch("parser.client")
    def test_injects_user_categories_into_prompt(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"expenses": [{
                "amount": 5000, "category": "mascota", "currency": "COP",
                "payment_method": None, "merchant": None, "description": "croquetas",
            }]})))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        parse_expense("croquetas 5000", categories=["mascota", "comida"])

        # The custom categories must appear in the system prompt sent to the model
        call = mock_client.chat.completions.create.call_args
        system_content = call.kwargs["messages"][0]["content"]
        assert "mascota" in system_content

    @patch("parser.client")
    def test_parses_expense_with_payment_method(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"expenses": [{
                "amount": 120000,
                "category": "mercado",
                "currency": "COP",
                "payment_method": "tarjeta",
                "merchant": None,
                "description": "mercado con tarjeta",
            }]})))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        result = parse_expense("mercado 120000 tarjeta")

        assert result[0]["amount"] == 120000
        assert result[0]["payment_method"] == "tarjeta"

    @patch("parser.client")
    def test_parses_multiple_expenses(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"expenses": [
                {"amount": 20000, "category": "comida", "currency": "COP",
                 "payment_method": None, "merchant": None, "description": "almuerzo"},
                {"amount": 40000, "category": "entretenimiento", "currency": "COP",
                 "payment_method": None, "merchant": None, "description": "cine"},
            ]})))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        result = parse_expense("almuerzo 20 luca y cine 40 mil")

        assert len(result) == 2
        assert result[0]["amount"] == 20000
        assert result[0]["category"] == "comida"
        assert result[1]["amount"] == 40000
        assert result[1]["category"] == "entretenimiento"
        assert "date" in result[0]
        assert "confidence" in result[1]


class TestBuildSystemPrompt:
    def test_uses_provided_categories(self):
        prompt = _build_system_prompt(["mascota", "viajes"])
        assert "mascota, viajes" in prompt

    def test_falls_back_to_defaults(self):
        prompt = _build_system_prompt(None)
        assert "comida" in prompt and "otro" in prompt

    def test_is_valid_no_brace_artifacts(self):
        # The f-string JSON examples must render real braces, not f-string escapes
        prompt = _build_system_prompt(["comida"])
        assert '{"expenses":' in prompt


class TestEstimateConfidence:
    def test_full_confidence(self):
        expense = {"amount": 32000, "category": "comida", "description": "almuerzo"}
        assert _estimate_confidence(expense) == 1.0

    def test_low_confidence_unknown_category(self):
        expense = {"amount": 5000, "category": "otro", "description": "algo"}
        assert _estimate_confidence(expense) == 0.7

    def test_zero_confidence_empty(self):
        assert _estimate_confidence({}) == 0.0
