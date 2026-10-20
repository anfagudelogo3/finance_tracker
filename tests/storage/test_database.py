from unittest.mock import patch, MagicMock

import pytest

from finance_tracker.storage.database import (
    save_message,
    save_expense,
    save_income,
    get_expenses,
    get_incomes,
    get_recent_records,
    update_record,
    delete_record,
    move_record,
    get_unreplaced_moves,
    restore_record,
    get_or_create_user,
    get_user_categories,
    change_category,
    get_category_overview,
    save_turn,
    find_replied_message,
    get_message_records,
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
        # Category lookup → the user's 'comida' row, INSERT RETURNING id, event RETURNING id
        mock_cursor.fetchone.side_effect = [
            {"id": 3, "name": "comida"}, {"id": 1}, {"id": 900}
        ]

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
        insert_sql, params = mock_cursor.execute.call_args_list[-2].args
        assert "INSERT INTO expenses" in insert_sql and params["category_id"] == 3

        # Same transaction: a 'create' event with the record's values
        event_sql, event = mock_cursor.execute.call_args_list[-1].args
        assert "INSERT INTO record_events" in event_sql
        assert (event["kind"], event["record_id"], event["action"]) == ("expense", 1, "create")
        assert event["before"] is None and event["message_id"] == 1
        assert event["source"] == "expense_agent"
        assert event["after"].adapted == {
            "amount": 32000.0, "currency": "COP", "category_id": 3, "category": "comida",
            "date": "2026-04-03", "payment_method": None, "merchant": None,
            "description": "almuerzo",
        }
        mock_get_conn.return_value.commit.assert_called_once()

    @patch("finance_tracker.storage.database.get_connection")
    def test_event_source_is_passed_through(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [{"id": 3, "name": "comida"}, {"id": 1}, {"id": 900}]
        expense = {"amount": 1, "category": "comida", "date": "2026-04-03",
                   "payment_method": None, "merchant": None, "description": "x", "confidence": 1}

        save_expense(1, expense, 7, event_source="image_parser")

        assert mock_cursor.execute.call_args_list[-1].args[1]["source"] == "image_parser"

    @patch("finance_tracker.storage.database.get_connection")
    def test_unknown_category_falls_back_to_otro(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchone.side_effect = [{"id": 9, "name": "otro"}, {"id": 1}, {"id": 900}]
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
        mock_cursor.fetchone.side_effect = [
            None, {"id": 11, "name": "otro"}, {"id": 1}, {"id": 900}
        ]
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
        mock_cursor.fetchone.side_effect = [{"id": 21, "name": "salario"}, {"id": 5}, {"id": 901}]
        income = {"amount": 2000000, "category": "salario", "date": "2026-04-03",
                  "description": "pago", "confidence": 1.0}

        assert save_income(message_id=1, income=income, user_id=7) == 5
        _, lookup_params = mock_cursor.execute.call_args_list[0].args
        assert lookup_params["kind"] == "income"
        assert income["category_id"] == 21
        event = mock_cursor.execute.call_args_list[-1].args[1]
        assert (event["kind"], event["record_id"], event["action"]) == ("income", 5, "create")
        assert event["source"] == "income_agent"
        assert set(event["after"].adapted) == {
            "amount", "currency", "category_id", "category", "date", "description"
        }


class TestGetIncomes:
    @patch("finance_tracker.storage.database.get_connection")
    def test_live_incomes_with_current_category_name(self, mock_get_conn):
        mock_cursor = _mock_connection(mock_get_conn)
        mock_cursor.fetchall.return_value = [{"amount": 1, "category": "salario"}]

        assert get_incomes(7, "2026-10-01", "2026-10-31") == [{"amount": 1, "category": "salario"}]
        sql, params = mock_cursor.execute.call_args.args
        assert "JOIN categories" in sql and "deleted_at IS NULL" in sql
        assert params == {"user_id": 7, "min_date": "2026-10-01", "max_date": "2026-10-31"}


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


def _categories(mock_cursor, *rows):
    """The FOR UPDATE category read at the start of change_category."""
    mock_cursor.fetchall.return_value = [{"id": i, "name": n, "active": a} for i, n, a in rows]


def _sqls(mock_cursor):
    return [c.args[0] for c in mock_cursor.execute.call_args_list]


def _events(mock_cursor):
    return [
        c.args[1]
        for c in mock_cursor.execute.call_args_list
        if "INSERT INTO category_events" in c.args[0]
    ]


class TestChangeCategory:
    @patch("finance_tracker.storage.database.get_connection")
    def test_add_new(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn, row_id=30)
        _categories(cur, (1, "comida", True))

        result = change_category(7, "expense", "add", "  Mascotas ")

        assert result == {"status": "added", "name": "mascotas"}
        assert _events(cur)[0]["action"] == "add" and _events(cur)[0]["category_id"] == 30

    @patch("finance_tracker.storage.database.get_connection")
    def test_add_reactivates_a_deactivated_name(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur, (5, "viajes", False))

        result = change_category(7, "expense", "add", "Viajes")

        assert result["status"] == "reactivated"
        assert any("active = TRUE" in q for q in _sqls(cur))
        assert _events(cur)[0]["action"] == "reactivate"

    @patch("finance_tracker.storage.database.get_connection")
    def test_rename_keeps_the_row(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur, (1, "comida", True))

        result = change_category(7, "expense", "rename", "comida", "Alimentación")

        assert result == {"status": "renamed", "name": "alimentación"}
        assert any("UPDATE categories SET name" in q for q in _sqls(cur))
        assert not any("UPDATE expenses" in q for q in _sqls(cur))  # records follow by id
        event = _events(cur)[0]
        assert (event["old_name"], event["new_name"]) == ("comida", "alimentación")

    @patch("finance_tracker.storage.database.get_connection")
    def test_rename_onto_existing_merges(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur, (1, "comida", True), (3, "mercado", True))
        cur.rowcount = 2

        result = change_category(7, "expense", "rename", "mercado", "comida")

        assert result == {"status": "merged", "name": "comida", "records": 4}
        moves = [c.args[1] for c in cur.execute.call_args_list if "SET category_id" in c.args[0]]
        assert moves == [{"target": 1, "source": 3, "user_id": 7}] * 2
        event = _events(cur)[0]
        assert event["action"] == "merge" and event["records"] == 4

    @patch("finance_tracker.storage.database.get_connection")
    def test_deactivate(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur, (2, "ropa", True))

        assert change_category(7, "expense", "deactivate", "ropa")["status"] == "deactivated"
        assert any("active = FALSE" in q for q in _sqls(cur))
        assert not any("DELETE" in q for q in _sqls(cur))

    @patch("finance_tracker.storage.database.get_connection")
    def test_otro_is_protected(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur, (4, "otro", True))

        assert change_category(7, "expense", "deactivate", "otro")["status"] == "protected"
        assert _events(cur) == []

    @patch("finance_tracker.storage.database.get_connection")
    def test_unknown_category(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        _categories(cur)
        assert change_category(7, "expense", "rename", "yates", "barcos")["status"] == "not_found"


class TestCategoryOverview:
    @patch("finance_tracker.storage.database.get_connection")
    def test_counts_live_records_of_both_tables(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchall.return_value = [
            {"id": 1, "name": "comida", "kind": "expense", "active": True, "records": 3}
        ]

        assert get_category_overview(7)[0]["records"] == 3
        sql = cur.execute.call_args.args[0]
        assert "FROM expenses" in sql and "FROM incomes" in sql and "deleted_at IS NULL" in sql


class TestReplyContextQueries:
    @patch("finance_tracker.storage.database.get_connection")
    def test_save_turn_stores_sid_and_message(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        save_turn(7, "assistant", "ok", external_id="SMbot", message_id=41)
        params = cur.execute.call_args.args[1]
        assert params["external_id"] == "SMbot" and params["message_id"] == 41

    @patch("finance_tracker.storage.database.get_connection")
    def test_users_own_message_is_found_first(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [
            {"message_id": 41, "raw_text": "", "transcript": "almuerzo veinte mil", "created_at": None}
        ]

        found = find_replied_message(7, "SMuser")

        assert found == {"role": "user", "text": "almuerzo veinte mil", "created_at": None, "message_id": 41}
        sql, params = cur.execute.call_args.args
        assert "FROM messages" in sql and "user_id = %(user_id)s" in sql
        assert params == {"user_id": 7, "sid": "SMuser"}

    @patch("finance_tracker.storage.database.get_connection")
    def test_then_the_bots_turns(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [None, {"content": "💸 Gasto…", "created_at": None, "message_id": 41}]

        found = find_replied_message(7, "SMbot")

        assert found["role"] == "assistant" and found["message_id"] == 41
        sql = cur.execute.call_args.args[0]
        assert "FROM conversation_turns" in sql and "user_id = %(user_id)s" in sql

    @patch("finance_tracker.storage.database.get_connection")
    def test_unknown_sid(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [None, None]
        assert find_replied_message(7, "SMnone") is None

    @patch("finance_tracker.storage.database.get_connection")
    def test_records_of_a_message_cover_both_tables(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchall.return_value = [{"kind": "expense", "id": 1}, {"kind": "income", "id": 2}]

        assert len(get_message_records(7, 41)) == 2
        sql, params = cur.execute.call_args.args
        assert "FROM expenses" in sql and "FROM incomes" in sql and "deleted_at IS NULL" in sql
        assert params == {"user_id": 7, "message_id": 41}


def _live_row(**overrides):
    return {
        "id": 12, "amount": 25000, "currency": "COP", "category_id": 3, "category": "comida",
        "date": "2026-10-20", "description": "almuerzo", "payment_method": None,
        "merchant": None, "created_at": None, **overrides,
    }


class TestRecordCorrections:
    @patch("finance_tracker.storage.database.get_connection")
    def test_recent_records_are_scoped_live_and_newest_first(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchall.return_value = []

        get_recent_records(7, "expense", "2026-08-23")

        sql, params = cur.execute.call_args.args
        assert "FROM expenses" in sql and "user_id = %(user_id)s" in sql
        assert "deleted_at IS NULL" in sql and "ORDER BY r.created_at DESC" in sql
        assert params["since"] == "2026-08-23" and params["user_id"] == 7

    @patch("finance_tracker.storage.database.get_connection")
    def test_update_logs_before_and_after_and_never_touches_created_at(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [_live_row(), {"id": 900}]

        result = update_record(7, "expense", 12, {"amount": 15000, "date": "2026-10-21"}, 88, "expense_agent")

        assert result["before"]["amount"] == 25000.0 and result["after"]["amount"] == 15000.0
        assert result["after"]["date"] == "2026-10-21"
        update_sql, update_params = cur.execute.call_args_list[1].args
        assert update_sql.startswith("UPDATE expenses SET")
        assert "expense_date = %(expense_date)s" in update_sql
        assert "created_at" not in update_sql
        assert "user_id = %(user_id)s" in update_sql and update_params["user_id"] == 7
        event = cur.execute.call_args_list[2].args[1]
        assert event["action"] == "update" and event["message_id"] == 88
        assert event["before"].adapted["amount"] == 25000.0

    @patch("finance_tracker.storage.database.get_connection")
    def test_update_category_must_be_an_active_category(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [_live_row(), None]
        with pytest.raises(ValueError):
            update_record(7, "expense", 12, {"category": "yates"}, 88, "expense_agent")
        mock_get_conn.return_value.commit.assert_not_called()

    @patch("finance_tracker.storage.database.get_connection")
    def test_update_rejects_fields_the_kind_does_not_have(self, mock_get_conn):
        _mock_connection(mock_get_conn)
        with pytest.raises(ValueError):
            update_record(7, "income", 5, {"merchant": "x"}, 88, "income_agent")

    @patch("finance_tracker.storage.database.get_connection")
    def test_update_of_another_users_or_deleted_record(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [None]
        with pytest.raises(LookupError):
            update_record(7, "expense", 12, {"amount": 1}, 88, "expense_agent")

    @patch("finance_tracker.storage.database.get_connection")
    def test_delete_is_soft_and_logged(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchone.side_effect = [_live_row(), {"id": 901}]

        before = delete_record(7, "expense", 12, 88, "expense_agent")

        assert before["amount"] == 25000.0
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("SET deleted_at = NOW()" in q for q in sqls)
        assert not any(q.lstrip().startswith("DELETE") for q in sqls)
        event = cur.execute.call_args_list[-1].args[1]
        assert event["action"] == "delete" and event["after"] is None

    @patch("finance_tracker.storage.database.get_connection")
    def test_message_records_include_records_a_correction_changed(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchall.return_value = []
        get_message_records(7, 41)
        sql = cur.execute.call_args.args[0]
        assert "FROM record_events" in sql


class TestRecordMoves:
    @patch("finance_tracker.storage.database.get_connection")
    def test_move_is_one_transaction_linking_the_new_record(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        # source row, its 'move' event, the category, the new row, its 'create' event
        cur.fetchone.side_effect = [
            _live_row(), {"id": 901}, {"id": 4, "name": "otro"}, {"id": 30}, {"id": 902},
        ]
        record = {"amount": 25000.0, "currency": "COP", "date": "2026-10-20",
                  "description": "almuerzo", "category": "otro"}

        result = move_record(7, "expense", 12, "income", record, 89, "income_agent")

        assert result["record"]["id"] == 30
        assert mock_get_conn.call_count == 1
        mock_get_conn.return_value.commit.assert_called_once()
        events = [c.args[1] for c in cur.execute.call_args_list
                  if "INSERT INTO record_events" in c.args[0]]
        assert events[0]["action"] == "move" and events[0]["record_id"] == 12
        assert events[1]["action"] == "create" and events[1]["related_event"] == 901

    @patch("finance_tracker.storage.database.get_connection")
    def test_unreplaced_moves_are_moves_nothing_points_back_to(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.fetchall.return_value = []
        get_unreplaced_moves(7, 22)
        sql, params = cur.execute.call_args.args
        assert "action = 'move'" in sql and "NOT EXISTS" in sql and "related_event" in sql
        assert params == {"user_id": 7, "message_id": 22}

    @patch("finance_tracker.storage.database.get_connection")
    def test_restore_clears_deleted_at_and_logs_an_undo(self, mock_get_conn):
        cur = _mock_connection(mock_get_conn)
        cur.rowcount = 1
        cur.fetchone.side_effect = [_live_row(), {"id": 903}]

        restore_record(7, "expense", 12, 22, "expense_agent", related_event=901)

        assert "SET deleted_at = NULL" in cur.execute.call_args_list[0].args[0]
        event = cur.execute.call_args_list[-1].args[1]
        assert event["action"] == "undo" and event["related_event"] == 901
