"""Corrections to existing expenses and incomes (Phase 8b), shared by both agents.

The expense and income agents keep recording new money, and also find, edit and delete
their own records with the tools here. Design: docs/design/phase-8-corrections.md.

**Ask until sure, decided in code.** The model only proposes which record and what
change; code decides whether it needs the user's "sí":

- an edit of a record the user swipe-replied to (its id is in `request.replied_to`) is
  exact: applied directly, and the reply shows what changed;
- an edit of a record found by search ("el último", "el Uber de ayer") is stored as a
  pending action with "¿Es este? …" (agents/pending.py), applied after "sí";
- a delete the user asked for always asks first.

**Moves between kinds (Phase 8c).** "Este no es un gasto, es un ingreso" is two steps by
two agents, combined by the router: the record's own agent deletes it with
reason="moved", the other agent records it again with `replaces=<id>`. Code makes the
pair behave as one operation:

- order: a record with `replaces` is refused until the source was moved out (or is
  waiting for confirmation) in this same message, so the router must delete first;
- confirmation: a move of the swipe-replied record applies directly; a record found by
  search waits for "¿Es este?", asked once the new record's category is known, and
  "sí" applies both steps in one transaction;
- fields: the new record copies amount, currency, date and description (and payment
  method / merchant) from the original; the receiving agent only picks the category;
- `settle_moves`, after the message: a record moved out that nothing replaced is
  restored, so a half-done move never loses or duplicates a record.

Replies are written here, from the tool results — the model never writes them.
"""

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from finance_tracker.agents import pending
from finance_tracker.config import (
    CORRECTION_WINDOW_DAYS,
    MSG_ERROR,
    MSG_RECORD_CONFIRM_DELETE,
    MSG_RECORD_CONFIRM_MOVE,
    MSG_RECORD_CONFIRM_UPDATE,
    MSG_RECORD_DELETED,
    MSG_RECORD_GONE,
    MSG_RECORD_MOVE_FAILED,
    MSG_RECORD_MOVED,
    MSG_RECORD_UPDATED,
    RECORD_FIELD_LABELS,
    TIMEZONE,
)
from finance_tracker.core import agent_loop
from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.core.confidence import estimate_confidence
from finance_tracker.core.dates import resolve_record_date
from finance_tracker.core.text import normalize
from finance_tracker.storage import database

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)
_LABELS = {
    "expense": ("gasto", "Gasto", "💸", "en"),
    "income": ("ingreso", "Ingreso", "💰", "de"),
}
_PLURALS = {"expense": "gastos", "income": "ingresos"}
_OTHER = {"expense": "income", "income": "expense"}
_SOURCES = {"expense": "expense_agent", "income": "income_agent"}

# Set when this message started a move, so settle_moves only reads the database then
_move_started: ContextVar[bool] = ContextVar("move_started", default=False)

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
    "description": "Delete one record by id. Confirmation with the user is handled automatically.",
    "input_schema": {
        "type": "object",
        "properties": {
            "record_id": {"type": "integer"},
            "reason": {
                "type": "string",
                "enum": ["user_asked", "moved"],
                "description": (
                    "user_asked: the user asked to delete it. moved: it was registered as "
                    "the wrong kind (gasto ↔ ingreso) and is re-recorded as the other kind."
                ),
            },
        },
        "required": ["record_id", "reason"],
    },
}


def replaces_property(kind: str) -> dict:
    """The `replaces` field of each new record in record_expenses / record_incomes."""
    other = _OTHER[kind]
    return {
        "type": ["integer", "null"],
        "description": (
            f"Only when this {kind} is an {other} being moved here (registered as the "
            f"wrong kind): that {other}'s id. Its amount, currency, date and description "
            "are copied from it; you choose the category. null otherwise."
        ),
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
    other = _OTHER[kind]
    other_label = _LABELS[other][0]
    return f"""
Corrections to existing {kind}s:
- If the message corrects or deletes an existing {label} ("no, eran 15000", "cámbialo a transporte", "borra el último {label}", "el Uber de ayer fue 12000"), do NOT record a new one. A specific past item with "fue / era / costó / eran" ("el almuerzo del lunes fue de 20 mil", "el mercado costó 90 mil, no 120") is a correction, not a new {label}.
- If you're not sure whether the message is a new {label} or a correction, call find_records first: if a matching record exists, treat it as a correction (ask_user if unsure which one); only record a new {label} when nothing matches.
- If <replied_to> lists a {kind} record, the user is pointing at it: use its id directly. If it lists several and you can't tell which, call ask_user.
- Otherwise call find_records first (last=1 for "el último"), then update_record or delete_record with the id you found. Several plausible matches → ask_user listing them briefly; none → ask_user.
- One match is enough: call the tool right away, never ask "¿es este?" yourself. The tools ask the user to confirm a record found by search automatically.
- update_record: set ONLY the fields the user asked to change; every other field must be null (don't copy the record's current values, don't touch the description unless asked).
- A {label} registered as the wrong kind ("este no es un {label}, es un {other_label}"): call delete_record with reason="moved" (find it first unless <replied_to> lists it). The {other} side records it again; don't record anything yourself.
- <moving_in> lists {other}s being moved here (registered as the wrong kind): call record_{kind}s once with one entry per record, replaces=<its id> and the category that fits; the other fields are copied from it. Don't search for it: it's not a {label} yet. The user already decided it is a {label}: never question that.
- Deleting many records at once is not allowed: call ask_user saying so.
- Always act through a tool, and ask through ask_user, in Spanish. Once the tools have done what the message asks, end your turn without writing anything: the reply to the user is written from what the tools did. Never call a tool twice for the same thing."""


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
        self.notes: list[str] = []  # for the router (AgentResponse.notes)
        self._asked: dict | None = None
        self._searched = False
        self._incoming: dict[int, dict] | None = None
        self._incoming_shown = False

    def check_new_record(self, items: list[dict]) -> None:
        """Before new records are saved (the model gets the error and corrects itself;
        a duplicate record is worse than one more Claude call):

        - while another record is being moved here in this message, the first new
          record without `replaces` is refused once, naming the id to use;
        - if the message reads like a correction and nothing was searched yet, refuse.
        """
        if all(item.get("replaces") for item in items):
            return
        incoming = self.incoming_moves()
        if incoming and not self._incoming_shown:
            self._incoming_shown = True
            ids = ", ".join(f"#{record_id}" for record_id in incoming)
            raise ValueError(
                f"This message is moving {_OTHER[self.kind]} {ids} here: record it with "
                f"replaces=<id> (its fields are copied). Record other {self.kind}s only "
                "if the user also mentioned new ones."
            )
        if self._searched or not _CORRECTION_WORDS.search(normalize(self.request.text)):
            return
        raise ValueError(
            "This message may correct an existing record. Before recording a new one, "
            "update the record in <replied_to> or call find_records; record a new one "
            "only if nothing matches."
        )

    def incoming_moves(self) -> dict[int, dict]:
        """Records of the other kind this message is moving here, by id: {"event_id",
        "before"} once moved out, {"pending": action} while waiting for "¿Es este?".
        The database is only read once this message started a move (the router runs
        the agents one after another), so a plain new record costs nothing extra."""
        if not _move_started.get():
            return {}
        if self._incoming is None:
            user_id, message_id = self.request.user_id, self.request.message_id
            other = _OTHER[self.kind]
            self._incoming = {
                m["record_id"]: m
                for m in database.get_unreplaced_moves(user_id, message_id)
                if m["kind"] == other
            }
            action = database.get_user_prefs(user_id).get("pending_action") or {}
            if _waiting_move(action, message_id) and action["to_kind"] == self.kind:
                self._incoming[action["record_id"]] = {"pending": action}
        return self._incoming

    def moving_in_block(self) -> str:
        """For the agent's input: the records this message is moving here, so it
        records them with `replaces` instead of searching its own kind. Empty when no
        move started (no database read)."""
        lines = []
        other = _OTHER[self.kind]
        for record_id, move in self.incoming_moves().items():
            record = move.get("before") or database.get_record(
                self.request.user_id, other, record_id
            )
            if record:
                lines.append(
                    f"{other} #{record_id}: {describe(other, record)} — record it with "
                    f"replaces={record_id}"
                )
        if not lines:
            return ""
        return "<moving_in>\n" + "\n".join(lines) + "\n</moving_in>"

    def replace(self, item: dict) -> str:
        """Record `item` as the new record of a move (`replaces` = the other kind's id).
        Refused unless that record was moved out, or is waiting for confirmation, in
        this message — the order is enforced here, not trusted to the router."""
        source_id = int(item["replaces"])
        other = _OTHER[self.kind]
        move = self.incoming_moves().get(source_id)
        if move is None:
            raise ValueError(
                f"{other} #{source_id} isn't being moved in this message: first the "
                f"{other} side must delete it with reason='moved', then record it here "
                f"with replaces={source_id}."
            )
        category = item.get("category") or "otro"
        del self._incoming[source_id]

        if "pending" in move:
            # Found by search: ask now that the category is known; "sí" moves it
            record = database.get_record(self.request.user_id, other, source_id)
            if record is None:
                raise LookupError(f"{other} #{source_id} doesn't exist")
            question = MSG_RECORD_CONFIRM_MOVE.format(
                record=describe(other, record),
                to_label=_PLURALS[self.kind],
                category=category,
            )
            pending.ask(
                self.request.user_id,
                {**move["pending"], "category": category},
                question,
                self.request.now,
                _SOURCES[self.kind],
            )
            self.replies.append(question)
            return "The user is asked to confirm the move; nothing else to do."

        record = _moved_record(self.kind, move["before"], category, item)
        if self.kind == "expense":
            record["source"] = self.request.message_type
        database.save_record(
            self.kind,
            self.request.message_id,
            record,
            self.request.user_id,
            _SOURCES[self.kind],
            related_event=move["event_id"],
        )
        self.replies.append(
            MSG_RECORD_MOVED.format(
                to_label=_PLURALS[self.kind], record=describe(self.kind, record)
            )
        )
        return "Recorded: the move is complete."

    def execute(self, name: str, tool_input: dict) -> str:
        if name == "find_records":
            self._searched = True
            return json.dumps(self._find(tool_input), ensure_ascii=False, default=str)
        if name == "update_record":
            return self._update(
                int(tool_input["record_id"]), tool_input.get("changes") or {}
            )
        if name == "delete_record":
            return self._delete(
                int(tool_input["record_id"]), tool_input.get("reason") or "user_asked"
            )
        if name == "ask_user":
            logger.info("The %s agent asks: %s", self.kind, tool_input["question"])
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

    def _delete(self, record_id: int, reason: str) -> str:
        record = database.get_record(self.request.user_id, self.kind, record_id)
        if record is None:
            raise LookupError(f"{self.kind} #{record_id} doesn't exist")
        if reason == "moved":
            return self._move_out(record_id)
        action = {"tool": "delete_record", "kind": self.kind, "record_id": record_id}
        question = MSG_RECORD_CONFIRM_DELETE.format(
            label=_LABELS[self.kind][0], record=describe(self.kind, record)
        )
        return self._ask(action, question)

    def _move_out(self, record_id: int) -> str:
        """First step of a move. No question of its own: a swipe-replied record is
        removed now; a searched one waits, and "¿Es este?" is asked when the other side
        records it (with its category). Either way the router hears what comes next."""
        other = _OTHER[self.kind]
        _move_started.set(True)
        next_step = (
            f"the {other} side must now record it with replaces={record_id} "
            "(its fields are copied)"
        )
        if record_id in self.exact_ids:
            database.move_out_record(
                self.request.user_id,
                self.kind,
                record_id,
                self.request.message_id,
                _SOURCES[self.kind],
            )
            self.notes.append(
                f"{self.kind} #{record_id} removed to be moved: {next_step}."
            )
            return "Removed to be moved. You're done."

        action = {
            "tool": "move_record",
            "kind": self.kind,
            "record_id": record_id,
            "to_kind": other,
            "message_id": self.request.message_id,
        }
        if self._asked not in (None, action):
            raise RuntimeError("only one change needing confirmation per message")
        if self._asked is None:
            # No question yet: it's written once the category is known (replace())
            pending.ask(
                self.request.user_id, action, "", self.request.now, _SOURCES[self.kind]
            )
            self._asked = action
        self.notes.append(
            f"{self.kind} #{record_id} will be moved once the user confirms: {next_step}, "
            "so the question can be sent."
        )
        return "Waiting for the user's confirmation. You're done."

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


def _waiting_move(action: dict, message_id: int) -> bool:
    """A move asked in this message that still lacks its new record's category."""
    return (
        action.get("tool") == "move_record"
        and action.get("message_id") == message_id
        and not action.get("category")
    )


def _moved_record(to_kind: str, before: dict, category: str, item: dict) -> dict:
    """The new record of a move: the original's fields, the receiving side's category.
    An income has no payment method or merchant; an expense takes them from the
    original when it had them, else from what the agent passed."""
    record = {
        "amount": before["amount"],
        "currency": before.get("currency") or "COP",
        "date": str(before["date"]),
        "description": before.get("description"),
        "category": category,
    }
    if to_kind == "expense":
        for field in ("payment_method", "merchant"):
            record[field] = before.get(field) or item.get(field)
    record["confidence"] = estimate_confidence(record)
    return record


def agent_response(
    kind: str,
    request: AgentRequest,
    outcome,
    corrections: Corrections,
    parts: list[str],
    saved: list[dict],
    empty_reply: str,
) -> AgentResponse:
    """The expense / income agent's answer once its loop ends. Doing nothing is not ok:
    the router must not take an untouched record for a finished step, so it gets
    ok=False and why (the last tool error), and the model's own discarded text is
    logged."""
    parts = parts + corrections.replies
    notes = corrections.notes or None
    if parts or notes:
        return AgentResponse(
            ok=True, reply_text="\n\n".join(parts), data=saved or None, notes=notes
        )
    if outcome.status == agent_loop.END_TURN and outcome.final_text:
        # It answered in text instead of ask_user: that text is its question (as
        # model-written as ask_user's). The router hears that nothing changed.
        logger.info("The %s agent asked in text: %s", kind, outcome.final_text)
        return AgentResponse(
            ok=True,
            reply_text=outcome.final_text,
            notes=[f"Nothing was changed: the {kind} agent asked the user a question."],
        )
    errors = [c.result for c in outcome.tool_calls if c.is_error]
    logger.info(
        "Nothing done by the %s agent (message id=%d, loop=%s); its text: %s",
        kind,
        request.message_id,
        outcome.status,
        outcome.final_text or "(none)",
    )
    if outcome.status == agent_loop.END_TURN:
        return AgentResponse(
            ok=False,
            reply_text=empty_reply,
            error=errors[-1] if errors else "nothing to record or correct",
        )
    return AgentResponse(ok=False, reply_text=MSG_ERROR, error=outcome.status)


def start_message() -> None:
    """Called before routing a message (orchestrator): no move started yet."""
    _move_started.set(False)


def settle_moves(user_id: int, message_id: int) -> list[str]:
    """After the agents are done with a message: a record moved out that nothing
    replaced is restored, and a move still missing its category is cancelled, so the
    user never ends with the record in both kinds or in neither. Returns reply lines.
    Reads the database only when this message started a move."""
    if not _move_started.get():
        return []
    _move_started.set(False)
    lines = []
    for move in database.get_unreplaced_moves(user_id, message_id):
        kind = move["kind"]
        database.restore_record(
            user_id,
            kind,
            move["record_id"],
            message_id,
            _SOURCES[kind],
            move["event_id"],
        )
        logger.warning(
            "Move of %s #%d had no new record: restored", kind, move["record_id"]
        )
        lines.append(
            MSG_RECORD_MOVE_FAILED.format(
                record=describe(kind, move["before"]), to_label=_PLURALS[_OTHER[kind]]
            )
        )
    action = database.get_user_prefs(user_id).get("pending_action") or {}
    if _waiting_move(action, message_id):
        database.append_user_prefs(
            user_id, {"pending_action": None}, source=_SOURCES[action["kind"]]
        )
        logger.warning(
            "Move of %s #%d was never asked: cancelled",
            action["kind"],
            action["record_id"],
        )
        record = database.get_record(user_id, action["kind"], action["record_id"])
        if record:
            lines.append(
                MSG_RECORD_MOVE_FAILED.format(
                    record=describe(action["kind"], record),
                    to_label=_PLURALS[action["to_kind"]],
                )
            )
    return lines


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
    """Apply a record edit, delete or move the user confirmed (called by
    agents/pending.py). The confirming message is logged as the cause, so a swipe-reply
    to the bot's answer finds the record. Returns (reply, no preference changes)."""
    kind = action["kind"]
    try:
        if action["tool"] == "move_record":
            to_kind = action["to_kind"]
            before = database.get_record(user_id, kind, action["record_id"])
            if before is None:
                raise LookupError(f"{kind} #{action['record_id']} not found")
            result = database.move_record(
                user_id,
                kind,
                action["record_id"],
                to_kind,
                _moved_record(to_kind, before, action.get("category") or "otro", {}),
                message_id,
                _SOURCES[to_kind],
            )
            return (
                MSG_RECORD_MOVED.format(
                    to_label=_PLURALS[to_kind],
                    record=describe(to_kind, result["record"]),
                ),
                {},
            )
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
