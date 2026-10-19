import json
from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.agents import users
from finance_tracker.config import (
    MSG_ACTION_CANCELLED,
    MSG_CATEGORY_DEACTIVATED,
    MSG_CATEGORY_MERGED,
    MSG_CATEGORY_RENAMED,
    MSG_CONFIRM_CURRENCY,
    MSG_CONFIRM_DEACTIVATE,
    MSG_CONFIRM_MERGE,
    MSG_CURRENCY_CHANGED,
    MSG_ERROR,
)
from finance_tracker.core.agent_types import AgentRequest

NOW = "2026-10-20T09:00:00-05:00"

_OVERVIEW = [
    {"id": 1, "name": "comida", "kind": "expense", "active": True, "records": 12},
    {"id": 2, "name": "ropa", "kind": "expense", "active": True, "records": 0},
    {"id": 3, "name": "mercado", "kind": "expense", "active": True, "records": 4},
    {"id": 4, "name": "otro", "kind": "expense", "active": True, "records": 1},
    {"id": 5, "name": "viajes", "kind": "expense", "active": False, "records": 2},
    {"id": 6, "name": "salario", "kind": "income", "active": True, "records": 3},
]


def _request(text="", profile=None):
    return AgentRequest(
        user_id=7,
        message_id=42,
        phone="+573001234567",
        text=text,
        media=[],
        message_type="text",
        now=NOW,
        conversation=[],
        **({"profile": profile} if profile else {}),
    )


def _tool_use(name, tool_input, block_id="tu_1"):
    block = MagicMock(type="tool_use", id=block_id, input=tool_input)
    block.name = name
    return block


def _text(text):
    return MagicMock(type="text", text=text)


def _response(stop_reason, *blocks):
    return MagicMock(stop_reason=stop_reason, content=list(blocks))


@pytest.fixture
def claude():
    with patch.object(users, "client") as mock:
        yield mock


@pytest.fixture
def db():
    with (
        patch.object(
            users, "get_category_overview", return_value=_OVERVIEW
        ) as overview,
        patch.object(users, "change_category") as change,
        patch.object(users, "append_user_prefs") as append,
        patch.object(
            users, "get_user_prefs", return_value={"default_currency": "COP"}
        ) as prefs,
    ):
        change.side_effect = lambda u, kind, action, name, new_name=None: {
            "add": {"status": "added", "name": name},
            "rename": {"status": "renamed", "name": new_name},
            "deactivate": {"status": "deactivated", "name": name},
        }[action]
        yield MagicMock(overview=overview, change=change, append=append, prefs=prefs)


def _run(claude, tool_calls, final_text="", text="x", profile=None):
    claude.messages.create.side_effect = [
        _response(
            "tool_use",
            *[_tool_use(n, i, f"tu_{k}") for k, (n, i) in enumerate(tool_calls)],
        ),
        _response("end_turn", *([_text(final_text)] if final_text else [])),
    ]
    return users.handle(_request(text, profile))


def _tool_results(claude):
    """The tool_result contents sent back to Claude on the second call."""
    messages = claude.messages.create.call_args_list[1].kwargs["messages"]
    return [b for b in messages[-1]["content"]]


class TestHarmlessChanges:
    def test_add_category_applies_immediately(self, claude, db):
        response = _run(
            claude,
            [("add_category", {"kind": "expense", "name": "Mascotas"})],
            final_text="✅ Agregué mascotas.",
        )

        db.change.assert_called_once_with(7, "expense", "add", "Mascotas")
        assert response.ok and response.reply_text == "✅ Agregué mascotas."
        db.append.assert_not_called()

    def test_deactivating_an_unused_category_needs_no_confirmation(self, claude, db):
        _run(
            claude,
            [("deactivate_category", {"kind": "expense", "name": "ropa"})],
            "listo",
        )
        db.change.assert_called_once_with(7, "expense", "deactivate", "ropa")

    def test_renaming_an_unused_category_needs_no_confirmation(self, claude, db):
        _run(
            claude,
            [
                (
                    "rename_category",
                    {"kind": "expense", "name": "ropa", "new_name": "vestuario"},
                )
            ],
            "ok",
        )
        db.change.assert_called_once_with(7, "expense", "rename", "ropa", "vestuario")

    def test_get_settings_lists_counts_and_deactivated(self, claude, db):
        _run(claude, [("get_settings", {})], "Tienes…")
        settings = json.loads(_tool_results(claude)[0]["content"])
        assert "comida (12 registros)" in settings["categories"]["expense"]["active"]
        assert settings["categories"]["expense"]["deactivated"] == ["viajes"]
        assert settings["default_currency"] == "COP"


class TestConfirmation:
    def test_deactivating_a_used_category_asks_first(self, claude, db):
        response = _run(
            claude, [("deactivate_category", {"kind": "expense", "name": "Comida"})]
        )

        db.change.assert_not_called()
        pending = db.append.call_args.args[1]["pending_action"]
        assert pending["tool"] == "deactivate_category" and pending["name"] == "comida"
        assert pending["asked_at"] == NOW
        assert response.reply_text == MSG_CONFIRM_DEACTIVATE.format(
            name="comida", records=12
        )

    def test_rename_onto_existing_category_is_a_merge_and_asks(self, claude, db):
        response = _run(
            claude,
            [
                (
                    "rename_category",
                    {"kind": "expense", "name": "mercado", "new_name": "comida"},
                )
            ],
        )
        db.change.assert_not_called()
        assert response.reply_text == MSG_CONFIRM_MERGE.format(
            name="mercado", new_name="comida", records=4
        )

    def test_currency_change_always_asks(self, claude, db):
        response = _run(claude, [("set_default_currency", {"code": "usd"})])
        assert response.reply_text == MSG_CONFIRM_CURRENCY.format(old="COP", new="USD")
        assert db.append.call_args.args[1]["pending_action"]["code"] == "USD"

    def test_same_currency_is_unchanged(self, claude, db):
        _run(claude, [("set_default_currency", {"code": "COP"})], "Ya es COP")
        db.append.assert_not_called()

    def test_only_one_confirmation_per_message(self, claude, db):
        response = _run(
            claude,
            [
                ("deactivate_category", {"kind": "expense", "name": "comida"}),
                ("deactivate_category", {"kind": "expense", "name": "mercado"}),
            ],
        )
        assert db.append.call_count == 1
        assert _tool_results(claude)[1]["is_error"] is True
        assert response.reply_text.startswith("La categoría *comida*")

    def test_repeating_the_same_request_is_not_an_error(self, claude, db):
        response = _run(
            claude,
            [
                ("set_default_currency", {"code": "USD"}),
                ("set_default_currency", {"code": "usd"}),
            ],
        )
        assert db.append.call_count == 1
        assert not any(r.get("is_error") for r in _tool_results(claude))
        assert response.reply_text == MSG_CONFIRM_CURRENCY.format(old="COP", new="USD")

    def test_model_text_and_question_are_both_sent(self, claude, db):
        response = _run(
            claude,
            [
                ("add_category", {"kind": "expense", "name": "mascotas"}),
                ("deactivate_category", {"kind": "expense", "name": "comida"}),
            ],
            final_text="✅ Agregué mascotas.",
        )
        assert response.reply_text.startswith(
            "✅ Agregué mascotas.\n\nLa categoría *comida*"
        )


class TestGuards:
    def test_otro_is_protected(self, claude, db):
        _run(
            claude,
            [("deactivate_category", {"kind": "expense", "name": "otro"})],
            "No se puede",
        )
        db.change.assert_not_called()
        assert json.loads(_tool_results(claude)[0]["content"])["status"] == "protected"

    def test_unknown_category(self, claude, db):
        _run(
            claude,
            [("deactivate_category", {"kind": "expense", "name": "yates"})],
            "No existe",
        )
        assert json.loads(_tool_results(claude)[0]["content"])["status"] == "not_found"

    def test_business_details_need_a_business_profile(self, claude, db):
        _run(
            claude,
            [
                (
                    "update_business_details",
                    {"name": "X", "industry": None, "sells": None, "main_costs": None},
                )
            ],
            "No",
        )
        db.append.assert_not_called()

    def test_business_details_merge_into_the_profile(self, claude, db):
        business = {
            "type": "business",
            "business": {"industry": "restaurante", "sells": "almuerzos"},
        }
        db.prefs.return_value = {"profile": business}
        _run(
            claude,
            [
                (
                    "update_business_details",
                    {
                        "name": None,
                        "industry": None,
                        "sells": "almuerzos y domicilios",
                        "main_costs": None,
                    },
                )
            ],
            "✅",
        )
        profile = db.append.call_args.args[1]["profile"]
        assert profile["business"] == {
            "industry": "restaurante",
            "sells": "almuerzos y domicilios",
        }
        assert db.append.call_args.kwargs == {"source": "users_agent"}

    def test_no_tool_takes_a_user_id(self):
        for tool in users.TOOLS:
            assert "user_id" not in json.dumps(tool["input_schema"])

    def test_safeguard_with_nothing_done_is_an_error(self, claude, db):
        claude.messages.create.return_value = _response("max_tokens")
        response = users.handle(_request("x"))
        assert not response.ok and response.reply_text == MSG_ERROR


class TestResolvePending:
    def _prefs(self, **action):
        return {
            "default_currency": "COP",
            "pending_action": {"asked_at": "2026-10-20T08:50:00-05:00", **action},
        }

    def test_no_pending_is_a_noop(self, db):
        assert users.resolve_pending(7, {}, "sí", NOW) == (None, {})
        db.append.assert_not_called()

    def test_yes_applies_a_deactivation(self, db):
        prefs = self._prefs(tool="deactivate_category", kind="expense", name="comida")
        reply, prefs = users.resolve_pending(7, prefs, "Sí", NOW)
        db.change.assert_called_once_with(7, "expense", "deactivate", "comida")
        assert reply == MSG_CATEGORY_DEACTIVATED.format(name="comida")
        assert prefs["pending_action"] is None

    def test_yes_applies_a_rename(self, db):
        prefs = self._prefs(
            tool="rename_category",
            kind="expense",
            name="comida",
            new_name="alimentación",
        )
        reply, _ = users.resolve_pending(7, prefs, "1", NOW)
        assert reply == MSG_CATEGORY_RENAMED.format(old="comida", name="alimentación")

    def test_yes_reports_a_merge(self, db):
        db.change.side_effect = None
        db.change.return_value = {"status": "merged", "name": "comida", "records": 4}
        prefs = self._prefs(
            tool="rename_category", kind="expense", name="mercado", new_name="comida"
        )
        reply, _ = users.resolve_pending(7, prefs, "dale", NOW)
        assert reply == MSG_CATEGORY_MERGED.format(
            old="mercado", name="comida", records=4
        )

    def test_yes_changes_the_currency(self, db):
        reply, prefs = users.resolve_pending(
            7, self._prefs(tool="set_default_currency", code="USD"), "si", NOW
        )
        assert reply == MSG_CURRENCY_CHANGED.format(code="USD")
        assert db.append.call_args.args[1] == {
            "default_currency": "USD",
            "pending_action": None,
        }
        assert prefs["default_currency"] == "USD"

    def test_no_cancels(self, db):
        reply, prefs = users.resolve_pending(
            7, self._prefs(tool="set_default_currency", code="USD"), "no", NOW
        )
        assert reply == MSG_ACTION_CANCELLED
        assert db.append.call_args.args[1] == {"pending_action": None}
        db.change.assert_not_called()

    def test_anything_else_cancels_and_is_routed(self, db):
        prefs = self._prefs(tool="deactivate_category", kind="expense", name="comida")
        reply, prefs = users.resolve_pending(7, prefs, "almuerzo 20000", NOW)
        assert reply is None and prefs["pending_action"] is None
        db.change.assert_not_called()

    def test_a_late_yes_does_not_confirm(self, db):
        prefs = self._prefs(tool="deactivate_category", kind="expense", name="comida")
        reply, _ = users.resolve_pending(7, prefs, "sí", "2026-10-21T09:00:00-05:00")
        assert reply is None
        db.change.assert_not_called()
