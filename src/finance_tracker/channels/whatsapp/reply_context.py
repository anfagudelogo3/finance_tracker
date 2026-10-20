"""Reply context (Phase 7a): what a WhatsApp swipe-reply refers to.

Twilio only sends the quoted message's SID. We resolve it from our own database first —
the user's messages by `messages.whatsapp_message_id`, the bot's by
`conversation_turns.external_id` — and only fall back to the Twilio API when it isn't
stored. The records created from the quoted message (or from the message a quoted bot
reply answered) come with it, so a reply to "💸 Gasto registrado…" points at that exact
expense. Design: docs/design/phase-7a-reply-context.md.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from finance_tracker.channels.whatsapp.sender import fetch_message
from finance_tracker.config import REPLIED_TO_MAX_CHARS, TIMEZONE
from finance_tracker.storage.database import find_replied_message, get_message_records

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)


def resolve(user_id: int, phone: str, sid: str | None) -> dict | None:
    """Return the quoted message as {role, text, date, records, source}, or None.

    Never raises: a reply whose context can't be resolved is processed like any other
    message.
    """
    if not sid:
        return None
    try:
        found = find_replied_message(user_id, sid)
        if found is not None:
            records = (
                get_message_records(user_id, found["message_id"])
                if found["message_id"]
                else []
            )
            return _shape(
                found["role"], found["text"], found["created_at"], records, "database"
            )
        return _from_twilio(phone, sid)
    except Exception:
        logger.exception("Could not resolve replied-to message %s", sid)
        return None


def _from_twilio(phone: str, sid: str) -> dict | None:
    """Fallback for a SID we don't have stored ("in a perfect world never taken")."""
    logger.warning(
        "Replied-to message %s not in the database, fetching from Twilio", sid
    )
    fetched = fetch_message(sid)
    if fetched is None:
        return None
    user = f"whatsapp:{phone}"
    if user not in (fetched["from"], fetched["to"]):
        # Never show the model a message from another conversation
        logger.warning(
            "Replied-to message %s doesn't belong to this user, ignored", sid
        )
        return None
    role = "user" if fetched["from"] == user else "assistant"
    return _shape(role, fetched["body"], fetched["date_sent"], [], "twilio")


def _shape(role: str, text: str, when, records: list[dict], source: str) -> dict:
    text = (text or "").strip()
    if len(text) > REPLIED_TO_MAX_CHARS:
        text = text[:REPLIED_TO_MAX_CHARS].rstrip() + "…"
    return {
        "role": role,
        "text": text,
        "date": _iso(when),
        "records": [
            {
                "kind": r["kind"],
                "id": r["id"],
                "amount": float(r["amount"]),
                "currency": r["currency"],
                "category": r["category"],
                "date": _iso(r["date"]),
                "description": r.get("description") or "",
            }
            for r in records
        ],
        "source": source,
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_TZ)
        return value.isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
