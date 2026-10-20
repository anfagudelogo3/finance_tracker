import json
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.agents import pending, records
from finance_tracker.config import MSG_RECORD_GONE
from finance_tracker.core.agent_types import AgentRequest

NOW = "2026-10-22T09:00:00-05:00"  # a Thursday


def _row(record_id, amount, category, day, description="", merchant=None, created=0):
    return {
        "id": record_id,
        "amount": Decimal(amount),
        "currency": "COP",
        "category_id": 1,
        "category": category,
        "date": date.fromisoformat(day),
        "description": description,
        "payment_method": None,
        "merchant": merchant,
        "created_at": created,
    }


ROWS = [
    _row(16, 120000, "mercado", "2026-10-21", "mercado", created=5),
    _row(14, 14000, "transporte", "2026-10-21", "viaje en Uber", "Uber", created=4),
    _row(12, 25000, "comida", "2026-10-20", "almuerzo", created=3),
]


def _request(replied_ids=(), kind="expense"):
    replied = None
    if replied_ids:
        replied = {
            "role": "assistant",
            "text": "💸 …",
            "date": None,
            "source": "database",
            "records": [{"kind": kind, "id": i} for i in replied_ids],
        }
    return AgentRequest(
        user_id=7,
        message_id=88,
        phone="+573001234567",
        text="x",
        media=[],
        message_type="text",
        now=NOW,
        conversation=[],
        replied_to=replied,
    )


@pytest.fixture
def db():
    by_id = {r["id"]: r for r in ROWS}
    with (
        patch.object(
            records.database, "get_recent_records", return_value=list(ROWS)
        ) as recent,
        patch.object(
            records.database, "get_record", side_effect=lambda u, k, i: by_id.get(i)
        ) as get,
        patch.object(records.database, "update_record") as update,
        patch.object(records.database, "delete_record") as delete,
        patch.object(
            records.database,
            "get_user_categories",
            return_value=["comida", "transporte", "mercado", "otro"],
        ),
        patch.object(pending, "append_user_prefs") as append,
    ):
        update.side_effect = lambda u, k, i, changes, m, s: {
            "before": records.database._snapshot(k, by_id[i]),
            "after": {**records.database._snapshot(k, by_id[i]), **changes},
        }
        yield MagicMock(
            recent=recent, get=get, update=update, delete=delete, append=append
        )


class TestFind:
    def test_text_matches_category_description_or_merchant_without_accents(self, db):
        found = json.loads(
            records.Corrections("expense", _request()).execute(
                "find_records",
                {
                    "text": "UBER",
                    "amount": None,
                    "date_from": None,
                    "date_to": None,
                    "last": None,
                },
            )
        )
        assert [r["id"] for r in found] == [14]

    def test_last_one_is_the_most_recently_registered(self, db):
        found = json.loads(
            records.Corrections("expense", _request()).execute(
                "find_records",
                {
                    "text": None,
                    "amount": None,
                    "date_from": None,
                    "date_to": None,
                    "last": 1,
                },
            )
        )
        assert [r["id"] for r in found] == [16]

    def test_default_window_is_60_days(self, db):
        records.Corrections("expense", _request()).execute(
            "find_records",
            {
                "text": None,
                "amount": None,
                "date_from": None,
                "date_to": None,
                "last": None,
            },
        )
        assert db.recent.call_args.args == (7, "expense", "2026-08-23", None)

    def test_amount_filter(self, db):
        found = json.loads(
            records.Corrections("expense", _request()).execute(
                "find_records",
                {
                    "text": None,
                    "amount": 25000,
                    "date_from": None,
                    "date_to": None,
                    "last": None,
                },
            )
        )
        assert [r["id"] for r in found] == [12]


class TestUpdate:
    def test_swipe_reply_record_is_updated_directly(self, db):
        corrections = records.Corrections("expense", _request(replied_ids=[12]))

        corrections.execute(
            "update_record", {"record_id": 12, "changes": {"amount": 15000}}
        )

        db.update.assert_called_once_with(
            7, "expense", 12, {"amount": 15000}, 88, "expense_agent"
        )
        db.append.assert_not_called()
        assert corrections.replies == [
            "✏️ Gasto actualizado: monto COP 25.000 → COP 15.000\n"
            "💸 COP 15.000 en comida (2026-10-20, almuerzo)"
        ]

    def test_searched_record_asks_first(self, db):
        corrections = records.Corrections("expense", _request())

        corrections.execute(
            "update_record",
            {"record_id": 14, "changes": {"amount": 12000, "category": None}},
        )

        db.update.assert_not_called()
        pending_action = db.append.call_args.args[1]["pending_action"]
        assert (
            pending_action["tool"] == "update_record"
            and pending_action["record_id"] == 14
        )
        assert pending_action["changes"] == {"amount": 12000}
        assert corrections.replies[0].startswith(
            "¿Es este? 💸 COP 14.000 en transporte (2026-10-21, viaje en Uber)\n"
            "→ Lo cambio: monto COP 14.000 → COP 12.000"
        )

    def test_date_words_are_resolved_in_code(self, db):
        corrections = records.Corrections("expense", _request(replied_ids=[12]))
        corrections.execute(
            "update_record",
            {"record_id": 12, "changes": {"date_text": "ayer", "date": None}},
        )
        assert db.update.call_args.args[3] == {"date": "2026-10-21"}

    def test_future_date_is_refused(self, db):
        with pytest.raises(ValueError, match="future"):
            records.Corrections("expense", _request(replied_ids=[12])).execute(
                "update_record", {"record_id": 12, "changes": {"date": "2026-10-30"}}
            )

    def test_unknown_category_is_refused_before_asking(self, db):
        with pytest.raises(ValueError, match="comida, transporte"):
            records.Corrections("expense", _request()).execute(
                "update_record", {"record_id": 14, "changes": {"category": "yates"}}
            )
        db.append.assert_not_called()

    def test_category_is_matched_case_insensitively(self, db):
        records.Corrections("expense", _request(replied_ids=[12])).execute(
            "update_record", {"record_id": 12, "changes": {"category": "Transporte"}}
        )
        assert db.update.call_args.args[3] == {"category": "transporte"}

    def test_missing_record(self, db):
        with pytest.raises(LookupError):
            records.Corrections("expense", _request()).execute(
                "update_record", {"record_id": 999, "changes": {"amount": 1}}
            )

    def test_nothing_to_change(self, db):
        with pytest.raises(ValueError, match="no field"):
            records.Corrections("expense", _request(replied_ids=[12])).execute(
                "update_record", {"record_id": 12, "changes": {"amount": None}}
            )


class TestDeleteAndAsk:
    def test_delete_always_asks_even_for_a_swipe_reply(self, db):
        corrections = records.Corrections("expense", _request(replied_ids=[12]))

        corrections.execute("delete_record", {"record_id": 12})

        db.delete.assert_not_called()
        assert db.append.call_args.args[1]["pending_action"]["tool"] == "delete_record"
        assert corrections.replies[0].startswith(
            "¿Borro este gasto? 💸 COP 25.000 en comida (2026-10-20, almuerzo)"
        )

    def test_only_one_confirmation_per_message(self, db):
        corrections = records.Corrections("expense", _request())
        corrections.execute("delete_record", {"record_id": 12})
        with pytest.raises(RuntimeError):
            corrections.execute("delete_record", {"record_id": 14})
        corrections.execute(
            "delete_record", {"record_id": 12}
        )  # same request again: fine
        assert db.append.call_count == 1

    def test_ask_user(self, db):
        corrections = records.Corrections("expense", _request())
        corrections.execute("ask_user", {"question": "¿Cuál de los dos almuerzos?"})
        assert corrections.replies == ["¿Cuál de los dos almuerzos?"]


class TestApplyConfirmed:
    def test_update_logs_the_confirming_message(self, db):
        action = {
            "tool": "update_record",
            "kind": "expense",
            "record_id": 14,
            "changes": {"amount": 12000},
        }
        reply, changes = records.apply_confirmed(7, action, 99)
        db.update.assert_called_once_with(
            7, "expense", 14, {"amount": 12000}, 99, "expense_agent"
        )
        assert (
            reply.startswith("✏️ Gasto actualizado: monto COP 14.000 → COP 12.000")
            and changes == {}
        )

    def test_delete(self, db):
        db.delete.return_value = records.database._snapshot("expense", ROWS[2])
        reply, _ = records.apply_confirmed(
            7, {"tool": "delete_record", "kind": "expense", "record_id": 12}, 99
        )
        db.delete.assert_called_once_with(7, "expense", 12, 99, "expense_agent")
        assert (
            reply == "🗑️ Gasto borrado: 💸 COP 25.000 en comida (2026-10-20, almuerzo)"
        )

    def test_record_gone_since_the_question(self, db):
        db.delete.side_effect = LookupError("gone")
        reply, _ = records.apply_confirmed(
            7, {"tool": "delete_record", "kind": "expense", "record_id": 12}, 99
        )
        assert reply == MSG_RECORD_GONE

    def test_pending_resolve_dispatches_record_actions(self, db):
        db.delete.return_value = records.database._snapshot("expense", ROWS[2])
        prefs = {
            "pending_action": {
                "tool": "delete_record",
                "kind": "expense",
                "record_id": 12,
                "asked_at": "2026-10-22T08:55:00-05:00",
                "source": "expense_agent",
            }
        }

        reply, prefs = pending.resolve(7, prefs, "sí", NOW, message_id=99)

        assert reply.startswith("🗑️ Gasto borrado")
        assert prefs["pending_action"] is None
        assert db.append.call_args.kwargs == {"source": "expense_agent"}


def test_income_tools_have_no_expense_only_fields():
    props = records.update_tool("income")["input_schema"]["properties"]["changes"][
        "properties"
    ]
    assert "payment_method" not in props and "merchant" not in props


def test_no_tool_takes_a_user_id():
    for tool in records.tools("expense") + records.tools("income"):
        assert "user_id" not in json.dumps(tool["input_schema"])


def test_unchanged_values_are_dropped(db):
    records.Corrections("expense", _request(replied_ids=[12])).execute(
        "update_record",
        {
            "record_id": 12,
            "changes": {
                "amount": 15000,
                "currency": "COP",
                "category": "comida",
                "description": "almuerzo",
            },
        },
    )
    assert db.update.call_args.args[3] == {"amount": 15000}


def test_no_real_change_is_an_error_for_the_model(db):
    with pytest.raises(ValueError, match="already"):
        records.Corrections("expense", _request(replied_ids=[12])).execute(
            "update_record", {"record_id": 12, "changes": {"amount": 25000}}
        )


class TestSearchBeforeRecording:
    """"el almuerzo del lunes fue de 20 mil" can't become a new record unsearched."""

    def _corrections(self, text):
        from dataclasses import replace

        return records.Corrections("expense", replace(_request(), text=text))

    def test_correction_words_require_a_search_first(self, db):
        with pytest.raises(ValueError, match="find_records"):
            self._corrections("el almuerzo del lunes fue de 20 mil").check_new_record()

    def test_after_a_search_a_new_record_is_allowed(self, db):
        corrections = self._corrections("el mercado costó 90 mil")
        corrections.execute(
            "find_records",
            {"text": "mercado", "amount": None, "date_from": None, "date_to": None, "last": None},
        )
        corrections.check_new_record()  # no error

    def test_plain_new_record_needs_no_search(self, db):
        self._corrections("almuerzo 20000").check_new_record()
        db.recent.assert_not_called()


def test_text_falls_back_to_any_word(db):
    found = json.loads(
        records.Corrections("expense", _request()).execute(
            "find_records",
            {"text": "almuerzo lunes", "amount": None, "date_from": None, "date_to": None, "last": None},
        )
    )
    assert [r["id"] for r in found] == [12]
