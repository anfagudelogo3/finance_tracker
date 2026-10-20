"""Moving a record between expenses and incomes (Phase 8c): two steps by two agents,
held together in code — order, confirmation, copied fields, and settling a half move."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from finance_tracker.agents import pending, records
from finance_tracker.config import (
    MSG_RECORD_CONFIRM_MOVE,
    MSG_RECORD_MOVE_FAILED,
    MSG_RECORD_MOVED,
)
from finance_tracker.core import agent_loop
from finance_tracker.core.agent_types import AgentRequest

NOW = "2026-10-20T13:45:00-05:00"
MESSAGE = 22

EXPENSE = {
    "id": 5,
    "amount": Decimal(99800),
    "currency": "COP",
    "category_id": 3,
    "category": "otro",
    "date": date(2026, 10, 18),
    "description": "transferencia de dinero",
    "payment_method": "nequi",
    "merchant": None,
}


class FakeDB:
    """Just enough of storage/database.py for a move: live records, record_events with
    'move' links, and the user's pending action."""

    def __init__(self):
        self.live = {("expense", 5): dict(EXPENSE)}
        self.gone = {}
        self.events = []  # {"id", "kind", "record_id", "action", "before", "related"}
        self.prefs = {}
        self.saved = []

    def _event(self, kind, record_id, action, before=None, related=None):
        event = {
            "id": len(self.events) + 1,
            "kind": kind,
            "record_id": record_id,
            "action": action,
            "before": before,
            "related": related,
        }
        self.events.append(event)
        return event["id"]

    def get_record(self, user_id, kind, record_id):
        record = self.live.get((kind, record_id))
        return dict(record) if record else None

    def move_out_record(self, user_id, kind, record_id, message_id, source):
        record = self.live.pop((kind, record_id))
        self.gone[(kind, record_id)] = record
        before = {
            **record,
            "amount": float(record["amount"]),
            "date": str(record["date"]),
        }
        return {
            "before": before,
            "event_id": self._event(kind, record_id, "move", before),
        }

    def get_unreplaced_moves(self, user_id, message_id):
        linked = {e["related"] for e in self.events}
        return [
            {
                "event_id": e["id"],
                "kind": e["kind"],
                "record_id": e["record_id"],
                "before": e["before"],
            }
            for e in self.events
            if e["action"] == "move" and e["id"] not in linked
        ]

    def save_record(
        self, kind, message_id, record, user_id, source, related_event=None
    ):
        record["id"] = 100 + len(self.saved)
        self.saved.append((kind, record, related_event))
        self.live[(kind, record["id"])] = record
        self._event(kind, record["id"], "create", related=related_event)
        return record["id"]

    def restore_record(
        self, user_id, kind, record_id, message_id, source, related_event
    ):
        self.live[(kind, record_id)] = self.gone.pop((kind, record_id))
        self._event(kind, record_id, "undo", related=related_event)

    def move_record(
        self, user_id, from_kind, record_id, to_kind, record, message_id, source
    ):
        moved = self.move_out_record(user_id, from_kind, record_id, message_id, source)
        self.save_record(
            to_kind, message_id, record, user_id, source, moved["event_id"]
        )
        return {"before": moved["before"], "record": record}

    def get_user_prefs(self, user_id):
        return dict(self.prefs)

    def append_user_prefs(self, user_id, delta, source="manual", replace=False):
        self.prefs.update(delta)
        return 1


@pytest.fixture
def db():
    fake = FakeDB()
    names = [n for n in dir(FakeDB) if not n.startswith("_")]
    with (
        patch.multiple(records.database, **{n: getattr(fake, n) for n in names}),
        patch.object(pending, "append_user_prefs", fake.append_user_prefs),
    ):
        records.start_message()
        yield fake
    records.start_message()


def _request(swiped=True):
    return AgentRequest(
        user_id=11,
        message_id=MESSAGE,
        phone="+573014027381",
        text="este no es un gasto es un ingreso",
        media=[],
        message_type="text",
        now=NOW,
        conversation=[],
        replied_to=(
            {
                "role": "assistant",
                "text": "💸 …",
                "date": None,
                "source": "database",
                "records": [{"kind": "expense", "id": 5}],
            }
            if swiped
            else None
        ),
    )


def _move_out(swiped=True):
    expense_side = records.Corrections("expense", _request(swiped))
    result = expense_side.execute("delete_record", {"record_id": 5, "reason": "moved"})
    return expense_side, result


def _income_item(**extra):
    return {
        "amount": 1,
        "currency": "USD",
        "category": "freelance",
        "date": None,
        "date_text": None,
        "description": "otra cosa",
        "replaces": 5,
        **extra,
    }


class TestSwipedMove:
    def test_delete_then_create_moves_it_with_the_original_fields(self, db):
        expense_side, _ = _move_out()

        assert ("expense", 5) not in db.live
        assert expense_side.replies == []  # the one line comes from the other side
        assert "replaces=5" in expense_side.notes[0]

        income_side = records.Corrections("income", _request())
        income_side.replace(_income_item())

        kind, record, related = db.saved[0]
        assert kind == "income" and related == db.events[0]["id"]
        # The original's amount, currency, date and description; the agent's category
        assert (record["amount"], record["currency"], record["date"]) == (
            99800.0,
            "COP",
            "2026-10-18",
        )
        assert record["description"] == "transferencia de dinero"
        assert record["category"] == "freelance"
        assert income_side.replies == [
            MSG_RECORD_MOVED.format(
                to_label="ingresos", record=records.describe("income", record)
            )
        ]
        assert records.settle_moves(11, MESSAGE) == []

    def test_create_before_the_delete_is_refused(self, db):
        income_side = records.Corrections("income", _request())

        with pytest.raises(ValueError, match="delete it with reason='moved'"):
            income_side.replace(_income_item())
        assert db.saved == []

    def test_a_move_out_needs_no_confirmation(self, db):
        _move_out()
        assert "pending_action" not in db.prefs

    def test_income_to_expense_takes_payment_method_from_the_agent(self, db):
        db.live = {("income", 9): {**EXPENSE, "id": 9, "payment_method": None}}
        request = replace(
            _request(),
            replied_to={
                "role": "assistant",
                "text": "",
                "date": None,
                "source": "database",
                "records": [{"kind": "income", "id": 9}],
            },
        )
        records.Corrections("income", request).execute(
            "delete_record", {"record_id": 9, "reason": "moved"}
        )
        records.Corrections("expense", request).replace(
            {
                "category": "transporte",
                "replaces": 9,
                "payment_method": "tarjeta",
                "merchant": "Uber",
            }
        )
        _, record, _ = db.saved[0]
        assert (record["payment_method"], record["merchant"]) == ("tarjeta", "Uber")


class TestSearchedMove:
    def test_asks_es_este_with_the_category_then_moves_on_yes(self, db):
        expense_side, _ = _move_out(swiped=False)

        assert ("expense", 5) in db.live  # nothing moved yet
        assert db.prefs["pending_action"]["tool"] == "move_record"
        assert expense_side.replies == []  # asked once the category is known

        income_side = records.Corrections("income", _request(swiped=False))
        income_side.replace(_income_item(category="regalo"))

        question = MSG_RECORD_CONFIRM_MOVE.format(
            record=records.describe("expense", EXPENSE),
            to_label="ingresos",
            category="regalo",
        )
        assert income_side.replies == [question]
        action = db.prefs["pending_action"]
        assert action["category"] == "regalo" and action["question"] == question
        assert db.saved == []
        assert records.settle_moves(11, MESSAGE) == []  # complete: nothing to settle

        reply, _ = pending.resolve(11, db.prefs, "sí", NOW, message_id=23)

        kind, record, related = db.saved[0]
        assert (kind, record["category"], record["amount"]) == (
            "income",
            "regalo",
            99800.0,
        )
        assert related is not None
        assert ("expense", 5) not in db.live
        assert reply.startswith("🔄 Pasado a ingresos")

    def test_no_keeps_the_record(self, db):
        _move_out(swiped=False)
        records.Corrections("income", _request(swiped=False)).replace(_income_item())

        pending.resolve(11, db.prefs, "no", NOW)

        assert ("expense", 5) in db.live and db.saved == []


class TestSettle:
    def test_a_record_moved_out_but_never_recreated_is_restored(self, db):
        _move_out()

        lines = records.settle_moves(11, MESSAGE)

        assert ("expense", 5) in db.live
        assert db.events[-1]["action"] == "undo"
        assert lines == [
            MSG_RECORD_MOVE_FAILED.format(
                record=records.describe("expense", db.events[0]["before"]),
                to_label="ingresos",
            )
        ]

    def test_a_move_never_asked_is_cancelled(self, db):
        _move_out(swiped=False)

        lines = records.settle_moves(11, MESSAGE)

        assert db.prefs["pending_action"] is None
        assert len(lines) == 1 and "ingresos" in lines[0]

    def test_no_move_no_database_reads(self):
        records.start_message()
        with patch.object(records.database, "get_unreplaced_moves") as moves:
            assert records.settle_moves(11, MESSAGE) == []
        moves.assert_not_called()


class TestGuards:
    def test_a_new_record_without_replaces_is_refused_once_while_one_moves_in(self, db):
        _move_out()
        income_side = records.Corrections("income", _request())
        plain = [{"amount": 99800, "category": "otro"}]

        with pytest.raises(ValueError, match="replaces=<id>"):
            income_side.check_new_record(plain)
        # Once shown, a genuinely new income is allowed (the correction-word guard aside)
        income_side._searched = True
        income_side.check_new_record(plain)

    def test_plain_records_read_nothing_when_no_move_started(self, db):
        with patch.object(records.database, "get_unreplaced_moves") as moves:
            records.Corrections("income", _request()).check_new_record(
                [{"amount": 1, "category": "otro"}]
            )
        moves.assert_not_called()

    def test_a_delete_the_user_asked_for_still_asks(self, db):
        side = records.Corrections("expense", _request())
        side.execute("delete_record", {"record_id": 5, "reason": "user_asked"})

        assert db.prefs["pending_action"]["tool"] == "delete_record"
        assert ("expense", 5) in db.live


class TestAgentResponse:
    def _outcome(self, *errors, text=""):
        outcome = agent_loop.LoopOutcome(status=agent_loop.END_TURN, final_text=text)
        outcome.tool_calls = [
            agent_loop.ToolCall("record_incomes", {}, error, True) for error in errors
        ]
        return outcome

    def test_a_text_answer_is_its_question_and_the_router_hears_nothing_changed(
        self, db
    ):
        side = records.Corrections("expense", _request())
        response = records.agent_response(
            "expense",
            _request(),
            self._outcome(text="¿Cuál almuerzo?"),
            side,
            [],
            [],
            "No entendí",
        )
        assert response.ok and response.reply_text == "¿Cuál almuerzo?"
        assert "Nothing was changed" in response.notes[0]

    def test_nothing_done_is_not_ok_and_says_why(self, db):
        side = records.Corrections("income", _request())
        response = records.agent_response(
            "income",
            _request(),
            self._outcome('{"error": "delete it first"}'),
            side,
            [],
            [],
            "No entendí",
        )
        assert not response.ok
        assert response.error == '{"error": "delete it first"}'

    def test_first_half_of_a_move_is_ok_with_notes_and_no_text(self, db):
        side, _ = _move_out()
        response = records.agent_response(
            "expense", _request(), self._outcome(), side, [], [], "No entendí"
        )
        assert response.ok and response.reply_text == ""
        assert response.notes == side.notes
