from unittest.mock import patch, MagicMock

import pytest

from finance_tracker.storage.database import (
    save_message,
    save_expense,
    save_income,
    get_expenses,
    get_or_create_user,
    get_user_categories,
    save_turn,
    load_recent_turns,
    get_user_prefs,
    append_user_prefs,
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
    @patch("finance_tracker.storage.database.get_connection")
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

    @patch("finance_tracker.storage.database.get_connection")
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
    @patch("finance_tracker.storage.database.get_connection")
    def test_saves_and_returns_id(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 1)
        # Category lookup → the user's 'comida' row, then INSERT RETURNING id
        mock_cursor.fetchone.side_effect = [{"id": 3, "name": "comida"}, {"id": 1}]

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
        assert expense["category_id"] == 3  # stored by id, not by name
        insert_sql, params = mock_cursor.execute.call_args_list[-1].args
        assert "category_id" in insert_sql and params["category_id"] == 3

    @patch("finance_tracker.storage.database.get_connection")
    def test_unknown_category_falls_back_to_otro(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [{"id": 9, "name": "otro"}, {"id": 1}]
        expense = {"amount": 5000, "category": "mascotas", "date": "2026-04-03",
                   "payment_method": None, "merchant": None, "description": "x", "confidence": 0.5}

        save_expense(message_id=1, expense=expense, user_id=7)

        lookup_sql, lookup_params = mock_cursor.execute.call_args_list[0].args
        assert "'otro'" in lookup_sql and lookup_params["user_id"] == 7
        assert expense["category_id"] == 9
        assert expense["category"] == "otro"  # the confirmation shows the stored name

    @patch("finance_tracker.storage.database.get_connection")
    def test_missing_otro_is_created_and_logged(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        # lookup → nothing; INSERT category RETURNING; INSERT expense RETURNING
        mock_cursor.fetchone.side_effect = [None, {"id": 11, "name": "otro"}, {"id": 1}]
        expense = {"amount": 5000, "category": None, "date": "2026-04-03",
                   "payment_method": None, "merchant": None, "description": "x", "confidence": 0.5}

        save_expense(message_id=1, expense=expense, user_id=7)

        sqls = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert any("INSERT INTO categories" in q for q in sqls)
        assert any("INSERT INTO category_events" in q for q in sqls)
        assert expense["category_id"] == 11


class TestSaveIncome:
    @patch("finance_tracker.storage.database.get_connection")
    def test_resolves_income_category(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [{"id": 21, "name": "salario"}, {"id": 5}]
        income = {"amount": 2000000, "category": "salario", "date": "2026-04-03",
                  "description": "pago", "confidence": 1.0}

        assert save_income(message_id=1, income=income, user_id=7) == 5
        _, lookup_params = mock_cursor.execute.call_args_list[0].args
        assert lookup_params["kind"] == "income"
        assert income["category_id"] == 21


class TestGetExpenses:
    @patch("finance_tracker.storage.database.get_connection")
    def test_reads_current_category_name_through_join(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = [{"amount": 1, "category": "alimentación"}]

        rows = get_expenses(7, "2026-04-01", "2026-04-30")

        sql = mock_cursor.execute.call_args.args[0]
        assert "JOIN categories" in sql and "c.name AS category" in sql
        assert rows == [{"amount": 1, "category": "alimentación"}]


class TestGetOrCreateUser:
    @patch("finance_tracker.storage.database.get_connection")
    def test_creates_new_user_and_seeds_categories(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn, 42)  # INSERT RETURNING id

        result = get_or_create_user("+573001234567")

        assert result == 42
        # New user → categories seeded via executemany, each logged as an 'add' event
        mock_cursor.executemany.assert_called_once()
        sqls = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert any("INSERT INTO category_events" in q and "'seed'" in q for q in sqls)

    @patch("finance_tracker.storage.database.get_connection")
    def test_returns_existing_user_without_seeding(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        # First fetchone (INSERT) → None (conflict), second (SELECT) → existing id
        mock_cursor.fetchone.side_effect = [None, {"id": 9}]

        result = get_or_create_user("+573001234567")

        assert result == 9
        mock_cursor.executemany.assert_not_called()


class TestGetUserCategories:
    @patch("finance_tracker.storage.database.get_connection")
    def test_returns_user_category_names(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = [{"name": "comida"}, {"name": "transporte"}]

        result = get_user_categories(1, kind="expense")

        assert result == ["comida", "transporte"]

    @patch("finance_tracker.storage.database.get_connection")
    def test_falls_back_to_defaults_when_empty(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = []

        result = get_user_categories(1, kind="expense")

        assert "comida" in result and "otro" in result  # default expense set

    @patch("finance_tracker.storage.database.get_connection")
    def test_income_fallback_uses_income_defaults(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = []

        result = get_user_categories(1, kind="income")

        assert "salario" in result


class TestConversationMemory:
    @patch("finance_tracker.storage.database.get_connection")
    def test_save_turn_executes_insert(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)

        save_turn(1, "user", "almuerzo 32000")

        mock_cursor.execute.assert_called_once()

    @patch("finance_tracker.storage.database.get_connection")
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


class TestUserPrefs:
    @patch("finance_tracker.storage.database.get_connection")
    def test_get_reads_latest_row(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.return_value = {"prefs": {"profile": {"type": "business"}}}

        result = get_user_prefs(7)

        assert result == {"profile": {"type": "business"}}
        sql = mock_cursor.execute.call_args[0][0]
        assert "FROM user_prefs" in sql
        assert "ORDER BY created_at DESC, id DESC" in sql
        assert "LIMIT 1" in sql

    @patch("finance_tracker.storage.database.get_connection")
    def test_get_returns_empty_dict_when_never_set(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.return_value = None

        assert get_user_prefs(7) == {}

    @patch("finance_tracker.storage.database.get_connection")
    def test_append_inserts_merged_snapshot_never_updates(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [
            {"prefs": {"profile": {"type": "personal"}, "default_currency": "COP"}},
            {"id": 55},  # RETURNING id
        ]
        changes = {"profile": {"type": "business", "business": {"industry": "restaurante", "description": "x"}}}

        prefs_id = append_user_prefs(7, changes, source="users_agent")

        assert prefs_id == 55
        statements = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert "FOR UPDATE" in statements[0]  # user row locked first
        assert not any("UPDATE user_prefs" in s for s in statements)
        insert_params = mock_cursor.execute.call_args_list[-1][0][1]
        assert insert_params["prefs"].adapted == {
            "profile": changes["profile"],
            "default_currency": "COP",  # untouched keys carried forward
        }
        assert insert_params["changes"].adapted == changes
        assert insert_params["source"] == "users_agent"

    @patch("finance_tracker.storage.database.get_connection")
    def test_append_first_change_starts_from_empty(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [None, {"id": 1}]

        append_user_prefs(7, {"default_currency": "USD"})

        insert_params = mock_cursor.execute.call_args_list[-1][0][1]
        assert insert_params["prefs"].adapted == {"default_currency": "USD"}
        assert insert_params["source"] == "manual"
