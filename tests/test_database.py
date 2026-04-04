from unittest.mock import patch, MagicMock

import pytest

from database import save_message, save_expense


def _mock_connection(mock_get_conn, row_id):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"id": row_id}

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    mock_get_conn.return_value = mock_conn
    return mock_cursor


class TestSaveMessage:
    @patch("database.get_connection")
    def test_saves_and_returns_id(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 1)

        result = save_message("wamid.123", "573001234567", "almuerzo 32000")

        assert result == 1
        mock_cursor.execute.assert_called_once()


class TestSaveExpense:
    @patch("database.get_connection")
    def test_saves_and_returns_id(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 1)

        expense = {
            "amount": 32000,
            "category": "comida",
            "date": "2026-04-03",
            "payment_method": None,
            "merchant": None,
            "description": "almuerzo",
            "confidence": 1.0,
        }

        result = save_expense(message_id=1, expense=expense)

        assert result == 1
        mock_cursor.execute.assert_called_once()
