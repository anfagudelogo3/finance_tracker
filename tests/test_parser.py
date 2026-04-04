import json
from unittest.mock import MagicMock, patch

import pytest

from parser import parse_expense, _estimate_confidence


class TestParseExpense:
    @patch("parser.client")
    def test_parses_simple_expense(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({
                "amount": 32000,
                "category": "comida",
                "payment_method": None,
                "merchant": None,
                "description": "almuerzo",
            })))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        result = parse_expense("almuerzo 32000")

        assert result["amount"] == 32000
        assert result["category"] == "comida"
        assert "date" in result
        assert "confidence" in result

    @patch("parser.client")
    def test_parses_expense_with_payment_method(self, mock_client):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({
                "amount": 120000,
                "category": "mercado",
                "payment_method": "tarjeta",
                "merchant": None,
                "description": "mercado con tarjeta",
            })))
        ]
        mock_client.chat.completions.create.return_value = mock_response

        result = parse_expense("mercado 120000 tarjeta")

        assert result["amount"] == 120000
        assert result["payment_method"] == "tarjeta"


class TestEstimateConfidence:
    def test_full_confidence(self):
        expense = {"amount": 32000, "category": "comida", "description": "almuerzo"}
        assert _estimate_confidence(expense) == 1.0

    def test_low_confidence_unknown_category(self):
        expense = {"amount": 5000, "category": "otro", "description": "algo"}
        assert _estimate_confidence(expense) == 0.7

    def test_zero_confidence_empty(self):
        assert _estimate_confidence({}) == 0.0
