from unittest.mock import patch, MagicMock

import pytest

from database import (
    save_message,
    save_expense,
    get_or_create_user,
    get_user_categories,
    save_turn,
    load_recent_turns,
)


def _mock_connection(mock_get_conn, row_id=1):
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

        result = save_message(
            user_id=1,
            whatsapp_message_id="wamid.123",
            phone_number="573001234567",
            raw_text="almuerzo 32000",
        )

        assert result == 1
        mock_cursor.execute.assert_called_once()

    @patch("database.get_connection")
    def test_returns_none_on_duplicate(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 1)
        mock_cursor.fetchone.return_value = None  # ON CONFLICT DO NOTHING returns no row

        result = save_message(
            user_id=1,
            whatsapp_message_id="wamid.123",
            phone_number="573001234567",
            raw_text="almuerzo 32000",
        )

        assert result is None


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

        result = save_expense(message_id=1, expense=expense, user_id=7)

        assert result == 1
        assert expense["user_id"] == 7  # user_id injected into the row
        mock_cursor.execute.assert_called_once()


class TestGetOrCreateUser:
    @patch("database.get_connection")
    def test_creates_new_user_and_seeds_categories(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 42)  # INSERT RETURNING id

        result = get_or_create_user("+573001234567")

        assert result == 42
        # New user → categories seeded via executemany
        mock_cursor.executemany.assert_called_once()

    @patch("database.get_connection")
    def test_returns_existing_user_without_seeding(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        # First fetchone (INSERT) → None (conflict), second (SELECT) → existing id
        mock_cursor.fetchone.side_effect = [None, {"id": 9}]

        result = get_or_create_user("+573001234567")

        assert result == 9
        mock_cursor.executemany.assert_not_called()


class TestGetUserCategories:
    @patch("database.get_connection")
    def test_returns_user_category_names(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = [{"name": "comida"}, {"name": "transporte"}]

        result = get_user_categories(1, kind="expense")

        assert result == ["comida", "transporte"]

    @patch("database.get_connection")
    def test_falls_back_to_defaults_when_empty(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = []

        result = get_user_categories(1, kind="expense")

        assert "comida" in result and "otro" in result  # default expense set

    @patch("database.get_connection")
    def test_income_fallback_uses_income_defaults(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = []

        result = get_user_categories(1, kind="income")

        assert "salario" in result


class TestConversationMemory:
    @patch("database.get_connection")
    def test_save_turn_executes_insert(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)

        save_turn(1, "user", "almuerzo 32000")

        mock_cursor.execute.assert_called_once()

    @patch("database.get_connection")
    def test_load_recent_turns_returns_chronological(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        # DB returns newest-first; function should reverse to oldest-first
        mock_cursor.fetchall.return_value = [
            {"role": "assistant", "content": "✅ Registré..."},
            {"role": "user", "content": "almuerzo 32000"},
        ]

        result = load_recent_turns(1)

        assert result[0]["content"] == "almuerzo 32000"
        assert result[1]["role"] == "assistant"
