"""Corrections to existing expenses and incomes (Phase 8b), shared by both agents.

The expense and income agents keep recording new money, and also find, edit and delete
their own records with the tools here. Design: docs/design/phase-8-corrections.md.

**Ask until sure, decided in code.** The model only proposes which record and what
change; code decides whether it needs the user's "sí":

- an edit of a record the user swipe-replied to (its id is in `request.replied_to`) is
  exact: applied directly, and the reply shows what changed;
- an edit of a record found by search ("el último", "el Uber de ayer") is stored as a
  pending action with "¿Es este? …" (agents/pending.py), applied after "sí";
- a delete always asks first.

Replies are written here, from the tool results — the model never writes them.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from finance_tracker.agents import pending
from finance_tracker.config import (
    CORRECTION_WINDOW_DAYS,
    MSG_RECORD_CONFIRM_DELETE,
    MSG_RECORD_CONFIRM_UPDATE,
    MSG_RECORD_DELETED,
    MSG_RECORD_GONE,
    MSG_RECORD_UPDATED,
    RECORD_FIELD_LABELS,
    TIMEZONE,
)
from finance_tracker.core.agent_types import AgentRequest
from finance_tracker.core.dates import resolve_record_date
from finance_tracker.core.text import normalize
from finance_tracker.storage import database

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)
_LABELS = {
    "expense": ("gasto", "Gasto", "💸", "en"),
    "income": ("ingreso", "Ingreso", "💰", "de"),
}
_SOURCES = {"expense": "expense_agent", "income": "income_agent"}

FIND_TOOL = {
    "name": "find_records",
    "description": (
        "Search the user's existing records of this kind (last 60 days unless dates are "
        "given), most recently registered first. Use last=1 for 'el último'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": ["string", "null"],
                "description": "Words to match in category, description or merchant (e.g. 'uber', 'almuerzo').",
            },
            "amount": {
                "type": ["number", "null"],
                "description": "The record's CURRENT amount, if the user said it. Never the corrected (new) amount; usually null.",
            },
            "date_from": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "date_to": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "last": {
                "type": ["integer", "null"],
                "description": "Only the N most recently registered.",
            },
        },
        "required": ["text", "amount", "date_from", "date_to", "last"],
    },
}
DELETE_TOOL = {
    "name": "delete_record",
    "description": "Delete one record by id (the user is always asked to confirm; handled automatically).",
    "input_schema": {
        "type": "object",
        "properties": {"record_id": {"type": "integer"}},
        "required": ["record_id"],
    },
}
ASK_TOOL = {
    "name": "ask_user",
    "description": "Ask the user ONE short question in Spanish when you can't tell what to do (which record, what to change, nothing found).",
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
}

_CHANGE_PROPS = {
    "amount": {"type": ["number", "null"]},
    "currency": {"type": ["string", "null"]},
    "category": {
        "type": ["string", "null"],
        "description": "One of the user's categories.",
    },
    "date_text": {
        "type": ["string", "null"],
        "description": "The words that give the new date (ayer, el 15).",
    },
    "date": {"type": ["string", "null"], "description": "The new date, YYYY-MM-DD."},
    "description": {"type": ["string", "null"]},
    "payment_method": {"type": ["string", "null"]},
    "merchant": {"type": ["string", "null"]},
}


def update_tool(kind: str) -> dict:
    props = dict(_CHANGE_PROPS)
    if kind == "income":
        props.pop("payment_method")
        props.pop("merchant")
    return {
        "name": "update_record",
        "description": "Change fields of one existing record by id. Only the fields the user asked to change; null for every other field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "integer"},
                "changes": {
                    "type": "object",
                    "properties": props,
                    "required": list(props),
                },
            },
            "required": ["record_id", "changes"],
        },
    }


def tools(kind: str) -> list[dict]:
    return [FIND_TOOL, update_tool(kind), DELETE_TOOL, ASK_TOOL]


def rules(kind: str) -> str:
    """The prompt section on corrections, appended to each agent's system prompt."""
    label = _LABELS[kind][0]
    other = "income" if kind == "expense" else "expense"
    return f"""
Corrections to existing {kind}s:
- If the message corrects or deletes an existing {label} ("no, eran 15000", "cámbialo a transporte", "borra el último {label}", "el Uber de ayer fue 12000"), do NOT record a new one. A specific past item with "fue / era / costó / eran" ("el almuerzo del lunes fue de 20 mil", "el mercado costó 90 mil, no 120") is a correction, not a new {label}.
- If you're not sure whether the message is a new {label} or a correction, call find_records first: if a matching record exists, treat it as a correction (ask_user if unsure which one); only record a new {label} when nothing matches.
- If <replied_to> lists a {kind} record, the user is pointing at it: use its id directly. If it lists several and you can't tell which, call ask_user.
- Otherwise call find_records first (last=1 for "el último"), then update_record or delete_record with the id you found. Several plausible matches → ask_user listing them briefly; none → ask_user.
- update_record: set ONLY the fields the user asked to change; every other field must be null (don't copy the record's current values, don't touch the description unless asked).
- If the user says this {label} was really an {other} (or the reverse), call ask_user saying that changing a record between gasto and ingreso isn't possible yet.
- Deleting many records at once is not allowed: call ask_user saying so.
- Always act through a tool. Once the tools have done what the message asks, end your turn without writing anything: the reply to the user is written from what the tools did. Never call a tool twice for the same thing."""


def describe(kind: str, record: dict) -> str:
    """'💸 COP 25.000 en comida (2026-10-20, almuerzo)'"""
    _, _, icon, prep = _LABELS[kind]
    details = str(record.get("date") or "")
    if record.get("description"):
        details += f", {record['description']}"
    return (
        f"{icon} {record.get('currency', 'COP')} {_amount(record['amount'])} "
        f"{prep} {record.get('category', 'otro')} ({details})"
    )


def describe_changes(before: dict, after: dict) -> str:
    """'monto COP 25.000 → COP 15.000; categoría comida → transporte'"""
    parts = []
    for field, label in RECORD_FIELD_LABELS.items():
        if field not in after or before.get(field) == after.get(field):
            continue
        old, new = before.get(field), after.get(field)
        if field == "amount":
            currency = after.get("currency") or before.get("currency", "")
            old, new = (
                f"{before.get('currency', currency)} {_amount(old)}",
                f"{currency} {_amount(new)}",
            )
        parts.append(f"{label} {old if old not in (None, '') else '—'} → {new}")
    return "; ".join(parts)


def _amount(value) -> str:
    return f"{float(value):,.0f}".replace(",", ".")


# "el almuerzo del lunes fue de 20 mil": words that describe an amount already spent or
# received, so the message may correct an existing record (normalized, no accents).
_CORRECTION_WORDS = re.compile(
    r"\b(fue|fueron|era|eran|costo|costaron|salio|salieron|valio|valieron)\b"
)


class Corrections:
    """Executes the correction tools for one agent request and collects the replies."""

    def __init__(self, kind: str, request: AgentRequest):
        self.kind = kind
        self.request = request
        self.message_date = datetime.fromisoformat(request.now).astimezone(_TZ).date()
        self.exact_ids = {
            r["id"]
            for r in (request.replied_to or {}).get("records", [])
            if r["kind"] == kind
        }
        self.replies: list[str] = []
        self._asked: dict | None = None
        self._searched = False

    def check_new_record(self) -> None:
        """Before a new record is saved: if the message reads like a correction and
        nothing was searched yet, refuse (the model gets the error and searches first).
        A duplicate record is worse than one more Claude call."""
        if self._searched or not _CORRECTION_WORDS.search(normalize(self.request.text)):
            return
        raise ValueError(
            "This message may correct an existing record. Before recording a new one, "
            "update the record in <replied_to> or call find_records; record a new one "
            "only if nothing matches."
        )

    def execute(self, name: str, tool_input: dict) -> str:
        if name == "find_records":
            self._searched = True
            return json.dumps(self._find(tool_input), ensure_ascii=False, default=str)
        if name == "update_record":
            return self._update(
                int(tool_input["record_id"]), tool_input.get("changes") or {}
            )
        if name == "delete_record":
            return self._delete(int(tool_input["record_id"]))
        if name == "ask_user":
            self.replies.append(tool_input["question"].strip())
            return "The question will be sent to the user."
        raise ValueError(f"unknown tool {name}")

    def _find(self, query: dict) -> list[dict]:
        window_start = self.message_date - timedelta(days=CORRECTION_WINDOW_DAYS)
        since = query.get("date_from") or window_start.isoformat()
        rows = database.get_recent_records(
            self.request.user_id, self.kind, since, query.get("date_to")
        )
        words = normalize(query.get("text") or "").split()
        if words:

            def haystack(r):
                return normalize(
                    " ".join(
                        str(r.get(f) or "")
                        for f in ("category", "description", "merchant")
                    )
                )

            # every word if possible; else any word ("almuerzo lunes" still finds lunches)
            matches = [r for r in rows if all(w in haystack(r) for w in words)]
            rows = matches or [r for r in rows if any(w in haystack(r) for w in words)]
        if query.get("amount"):
            rows = [r for r in rows if float(r["amount"]) == float(query["amount"])]
        if query.get("last"):
            rows = rows[: int(query["last"])]
        return [
            {
                k: (float(v) if k == "amount" else v)
                for k, v in r.items()
                if k not in ("category_id", "created_at")
            }
            for r in rows[:10]
        ]

    def _normalize_changes(self, changes: dict) -> dict:
        clean = {
            k: v for k, v in changes.items() if v not in (None, "") and k != "date_text"
        }
        if changes.get("date") or changes.get("date_text"):
            new_date, is_future = resolve_record_date(
                changes.get("date"), self.message_date, changes.get("date_text")
            )
            if is_future:
                raise ValueError("the new date is in the future; ask the user")
            clean["date"] = new_date.isoformat()
        if "currency" in clean:
            clean["currency"] = str(clean["currency"]).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", clean["currency"]):
                raise ValueError("currency must be an ISO 4217 code like COP or USD")
        if "category" in clean:
            # Checked now, not after the user's "sí"
            names = database.get_user_categories(self.request.user_id, kind=self.kind)
            match = next(
                (n for n in names if n.lower() == str(clean["category"]).lower()), None
            )
            if match is None:
                raise ValueError(f"category must be one of: {', '.join(names)}")
            clean["category"] = match
        if not clean:
            raise ValueError("no field to change")
        return clean

    def _update(self, record_id: int, changes: dict) -> str:
        changes = self._normalize_changes(changes)
        record = database.get_record(self.request.user_id, self.kind, record_id)
        if record is None:
            raise LookupError(f"{self.kind} #{record_id} doesn't exist")
        # The model sometimes repeats current values: keep only real changes
        changes = {k: v for k, v in changes.items() if not _same(record.get(k), v)}
        if not changes:
            raise ValueError("those are already the record's values: nothing to change")
        action = {
            "tool": "update_record",
            "kind": self.kind,
            "record_id": record_id,
            "changes": changes,
        }

        if record_id in self.exact_ids:
            result = database.update_record(
                self.request.user_id,
                self.kind,
                record_id,
                changes,
                self.request.message_id,
                _SOURCES[self.kind],
            )
            self.replies.append(_updated_reply(self.kind, result))
            return "Updated."

        preview = {**record, **changes}
        question = MSG_RECORD_CONFIRM_UPDATE.format(
            record=describe(self.kind, record),
            changes=describe_changes(record, preview),
        )
        return self._ask(action, question)

    def _delete(self, record_id: int) -> str:
        record = database.get_record(self.request.user_id, self.kind, record_id)
        if record is None:
            raise LookupError(f"{self.kind} #{record_id} doesn't exist")
        action = {"tool": "delete_record", "kind": self.kind, "record_id": record_id}
        question = MSG_RECORD_CONFIRM_DELETE.format(
            label=_LABELS[self.kind][0], record=describe(self.kind, record)
        )
        return self._ask(action, question)

    def _ask(self, action: dict, question: str) -> str:
        if self._asked == action:  # the model repeated the same request
            return "Needs confirmation: the question will be sent to the user."
        if self._asked is not None:
            raise RuntimeError("only one change needing confirmation per message")
        pending.ask(
            self.request.user_id,
            action,
            question,
            self.request.now,
            _SOURCES[self.kind],
        )
        self._asked = action
        self.replies.append(question)
        return "Needs confirmation: the question will be sent to the user. Not applied yet."


def _same(current, new) -> bool:
    if current is None:
        return False
    try:
        return float(current) == float(new)
    except (TypeError, ValueError):
        return str(current).strip().lower() == str(new).strip().lower()


def _updated_reply(kind: str, result: dict) -> str:
    return MSG_RECORD_UPDATED.format(
        Label=_LABELS[kind][1],
        changes=describe_changes(result["before"], result["after"]),
        record=describe(kind, result["after"]),
    )


def apply_confirmed(
    user_id: int, action: dict, message_id: int | None
) -> tuple[str, dict]:
    """Apply a record edit or delete the user confirmed (called by agents/pending.py).
    The confirming message is logged as the cause, so a swipe-reply to the bot's
    answer finds the record. Returns (reply, no preference changes)."""
    kind = action["kind"]
    try:
        if action["tool"] == "update_record":
            result = database.update_record(
                user_id,
                kind,
                action["record_id"],
                action["changes"],
                message_id,
                _SOURCES[kind],
            )
            return _updated_reply(kind, result), {}
        before = database.delete_record(
            user_id, kind, action["record_id"], message_id, _SOURCES[kind]
        )
        return (
            MSG_RECORD_DELETED.format(
                Label=_LABELS[kind][1], record=describe(kind, before)
            ),
            {},
        )
    except (LookupError, ValueError):
        # Deleted or changed since the question (or its category was deactivated)
        logger.info("Confirmed %s could not be applied", action["tool"], exc_info=True)
        return MSG_RECORD_GONE, {}
