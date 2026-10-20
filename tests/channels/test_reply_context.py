from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from finance_tracker.channels.whatsapp import reply_context

PHONE = "+573001234567"


@pytest.fixture
def deps():
    with (
        patch.object(reply_context, "find_replied_message") as find,
        patch.object(reply_context, "get_message_records", return_value=[]) as records,
        patch.object(reply_context, "fetch_message") as fetch,
    ):
        yield find, records, fetch


def _record(**overrides):
    return {
        "kind": "expense",
        "id": 12,
        "amount": Decimal("25000.00"),
        "currency": "COP",
        "category": "comida",
        "date": date(2026, 10, 20),
        "description": "almuerzo",
        **overrides,
    }


def test_not_a_reply(deps):
    find, _, fetch = deps
    assert reply_context.resolve(7, PHONE, None) is None
    find.assert_not_called()
    fetch.assert_not_called()


def test_bot_confirmation_resolves_to_the_records_of_the_message_it_answered(deps):
    find, records, fetch = deps
    find.return_value = {
        "role": "assistant",
        "text": "💸 Gasto registrado: COP 25.000 en comida (2026-10-20)",
        "created_at": datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc),
        "message_id": 41,
    }
    records.return_value = [_record()]

    result = reply_context.resolve(7, PHONE, "SMbot")

    find.assert_called_once_with(7, "SMbot")  # always scoped by the verified user
    records.assert_called_once_with(7, 41)
    fetch.assert_not_called()
    assert result["role"] == "assistant" and result["source"] == "database"
    assert result["date"] == "2026-10-20T12:00:00-05:00"  # Bogota
    assert result["records"] == [
        {
            "kind": "expense",
            "id": 12,
            "amount": 25000.0,
            "currency": "COP",
            "category": "comida",
            "date": "2026-10-20",
            "description": "almuerzo",
        }
    ]


def test_users_photo_is_described_by_its_records(deps):
    find, records, _ = deps
    find.return_value = {
        "role": "user",
        "text": "",
        "created_at": None,
        "message_id": 50,
    }
    records.return_value = [_record(category="mercado", amount=Decimal("85000"))]

    result = reply_context.resolve(7, PHONE, "SMphoto")

    assert result["text"] == "" and result["records"][0]["category"] == "mercado"


def test_turn_without_message_has_no_records(deps):
    find, records, _ = deps
    find.return_value = {
        "role": "assistant",
        "text": "hola",
        "created_at": None,
        "message_id": None,
    }
    assert reply_context.resolve(7, PHONE, "SMx")["records"] == []
    records.assert_not_called()


def test_long_text_is_truncated(deps):
    find, _, _ = deps
    find.return_value = {
        "role": "assistant",
        "text": "x" * 1000,
        "created_at": None,
        "message_id": None,
    }
    text = reply_context.resolve(7, PHONE, "SMx")["text"]
    assert len(text) <= reply_context.REPLIED_TO_MAX_CHARS + 1 and text.endswith("…")


class TestTwilioFallback:
    def test_used_only_when_not_stored(self, deps):
        find, _, fetch = deps
        find.return_value = None
        fetch.return_value = {
            "from": "whatsapp:+14155238886",
            "to": f"whatsapp:{PHONE}",
            "body": "¿Fue un gasto o un ingreso?",
            "date_sent": None,
        }

        result = reply_context.resolve(7, PHONE, "SMold")

        fetch.assert_called_once_with("SMold")
        assert result["role"] == "assistant" and result["source"] == "twilio"
        assert result["records"] == []

    def test_users_own_message(self, deps):
        find, _, fetch = deps
        find.return_value = None
        fetch.return_value = {
            "from": f"whatsapp:{PHONE}",
            "to": "whatsapp:+14155238886",
            "body": "almuerzo 20000",
            "date_sent": None,
        }
        assert reply_context.resolve(7, PHONE, "SMold")["role"] == "user"

    def test_message_of_another_user_is_ignored(self, deps):
        find, _, fetch = deps
        find.return_value = None
        fetch.return_value = {
            "from": "whatsapp:+573009999999",
            "to": "whatsapp:+14155238886",
            "body": "datos de otra persona",
            "date_sent": None,
        }
        assert reply_context.resolve(7, PHONE, "SMother") is None

    def test_fetch_failure_means_no_context(self, deps):
        find, _, fetch = deps
        find.return_value = None
        fetch.return_value = None
        assert reply_context.resolve(7, PHONE, "SMgone") is None


def test_any_error_means_no_context(deps):
    find, _, _ = deps
    find.side_effect = RuntimeError("db down")
    assert reply_context.resolve(7, PHONE, "SMx") is None
