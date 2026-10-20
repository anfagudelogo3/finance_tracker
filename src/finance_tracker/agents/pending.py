"""Changes waiting for the user's "sí": settings (Phase 6c) and records (Phase 8b).

An agent never applies a destructive or uncertain change inside its loop. It stores the
change as `pending_action` in user_prefs, with the question written in code, and ends
its turn. On the user's very next message `handler.py` calls `resolve` before any
routing:

- "sí" (or "1", "dale"…) applies it, with no LLM call;
- "no" cancels it with a short reply;
- anything else cancels it silently, and that message is routed normally;
- a question older than PENDING_ACTION_MINUTES no longer counts.

Each owner applies its own actions: `users.apply_confirmed` for settings,
`records.apply_confirmed` for expense/income edits and deletes.
"""

import logging
from datetime import datetime, timedelta

from finance_tracker.config import MSG_ACTION_CANCELLED, PENDING_ACTION_MINUTES
from finance_tracker.core.text import is_no, is_yes
from finance_tracker.storage.database import append_user_prefs

logger = logging.getLogger(__name__)

_RECORD_TOOLS = {"update_record", "delete_record", "move_record"}


def ask(user_id: int, action: dict, question: str, now: str, source: str) -> None:
    """Store `action` as the change waiting for confirmation (replacing any other)."""
    pending = {**action, "question": question, "asked_at": now, "source": source}
    append_user_prefs(user_id, {"pending_action": pending}, source=source)


def resolve(
    user_id: int, prefs: dict, text: str, now: str, message_id: int | None = None
) -> tuple[str | None, dict]:
    """Settle the pending change, if any, with this message.

    Returns (reply, prefs). reply is None when nothing was pending, or the message
    wasn't a "sí"/"no" (the change is cancelled and the message must be routed
    normally). `message_id` is the confirming message: it's recorded as the cause of
    the change, so a swipe-reply to the bot's "✅ …" finds the record.
    """
    action = prefs.get("pending_action")
    if not action:
        return None, prefs
    source = action.get("source", "users_agent")

    if not is_yes(text) or _expired(action, now):
        append_user_prefs(user_id, {"pending_action": None}, source=source)
        prefs = {**prefs, "pending_action": None}
        logger.info("Pending %s cancelled for user=%d", action["tool"], user_id)
        if is_no(text) and not _expired(action, now):
            return MSG_ACTION_CANCELLED, prefs
        return None, prefs

    # Imported here: both modules import this one to ask for confirmation
    if action["tool"] in _RECORD_TOOLS:
        from finance_tracker.agents import records

        reply, changes = records.apply_confirmed(user_id, action, message_id)
    else:
        from finance_tracker.agents import users

        reply, changes = users.apply_confirmed(user_id, action)

    append_user_prefs(user_id, {**changes, "pending_action": None}, source=source)
    logger.info("Pending %s applied for user=%d", action["tool"], user_id)
    return reply, {**prefs, **changes, "pending_action": None}


def _expired(action: dict, now: str) -> bool:
    try:
        asked = datetime.fromisoformat(action.get("asked_at") or "")
        return datetime.fromisoformat(now) - asked > timedelta(
            minutes=PENDING_ACTION_MINUTES
        )
    except ValueError:
        return True
