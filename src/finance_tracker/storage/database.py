import logging
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from finance_tracker.config import (
    DATABASE_URL,
    DEFAULT_EXPENSE_CATEGORIES,
    DEFAULT_INCOME_CATEGORIES,
    CONVERSATION_MAX_TURNS,
    CONVERSATION_WINDOW_MINUTES,
    TIMEZONE,
)

logger = logging.getLogger(__name__)


@contextmanager
def get_connection():
    # Session timezone = the app's, so CURRENT_DATE / NOW() agree with the code's dates
    conn = psycopg2.connect(
        DATABASE_URL, cursor_factory=RealDictCursor, options=f"-c timezone={TIMEZONE}"
    )
    try:
        yield conn
    finally:
        conn.close()


# ── Users & categories ───────────────────────────────────────────────────────────

def get_or_create_user(phone: str, name: str | None = None) -> int:
    """Return the user id for a phone, creating the user (and seeding default
    categories) on first contact. The phone is always the verified Twilio sender."""
    insert = """
        INSERT INTO users (phone, name)
        VALUES (%(phone)s, %(name)s)
        ON CONFLICT (phone) DO NOTHING
        RETURNING id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(insert, {"phone": phone, "name": name})
            row = cur.fetchone()
            if row is not None:
                user_id = row["id"]
                _seed_default_categories(cur, user_id)
                conn.commit()
                logger.info("Created new user id=%d phone=%s", user_id, phone)
                return user_id

            cur.execute("SELECT id FROM users WHERE phone = %(phone)s;", {"phone": phone})
            user_id = cur.fetchone()["id"]
            logger.debug("Existing user id=%d phone=%s", user_id, phone)
            return user_id


def _seed_default_categories(cur, user_id: int) -> None:
    """Insert the default expense + income categories for a brand-new user."""
    rows = (
        [(user_id, name, "expense") for name in DEFAULT_EXPENSE_CATEGORIES]
        + [(user_id, name, "income") for name in DEFAULT_INCOME_CATEGORIES]
    )
    cur.executemany(
        """
        INSERT INTO categories (user_id, name, kind)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, name, kind) DO NOTHING;
        """,
        rows,
    )
    # category_events is the append-only history of every category change
    cur.execute(
        """
        INSERT INTO category_events (user_id, category_id, kind, action, new_name, source)
        SELECT user_id, id, kind, 'add', name, 'seed' FROM categories
        WHERE user_id = %(user_id)s;
        """,
        {"user_id": user_id},
    )
    logger.debug("Seeded %d default categories for user id=%d", len(rows), user_id)


def _resolve_category(cur, user_id: int, kind: str, name: str | None) -> dict:
    """Return the user's category row ({id, name}) for a name the model picked.

    Records store category_id, never the name, so a rename is a single-row change. The
    agents constrain the model to the active names, but the legacy image parser doesn't:
    an unknown name falls back to the user's 'otro', which is created if it's missing.
    """
    cur.execute(
        """
        SELECT id, name FROM categories
        WHERE user_id = %(user_id)s AND kind = %(kind)s
          AND lower(name) IN (lower(%(name)s), 'otro')
        ORDER BY (lower(name) = lower(%(name)s)) DESC, active DESC
        LIMIT 1;
        """,
        {"user_id": user_id, "kind": kind, "name": name or "otro"},
    )
    row = cur.fetchone()
    if row is not None:
        return row
    cur.execute(
        """
        INSERT INTO categories (user_id, name, kind) VALUES (%(user_id)s, 'otro', %(kind)s)
        RETURNING id, name;
        """,
        {"user_id": user_id, "kind": kind},
    )
    row = cur.fetchone()
    cur.execute(
        """
        INSERT INTO category_events (user_id, category_id, kind, action, new_name, source)
        VALUES (%(user_id)s, %(category_id)s, %(kind)s, 'add', 'otro', 'seed');
        """,
        {"user_id": user_id, "category_id": row["id"], "kind": kind},
    )
    logger.warning("Created missing 'otro' %s category for user id=%d", kind, user_id)
    return row


def get_user_categories(user_id: int, kind: str = "expense") -> list[str]:
    """Return the active category names for a user. Falls back to the defaults if
    none are found (should only happen if seeding was skipped)."""
    query = """
        SELECT name FROM categories
        WHERE user_id = %(user_id)s AND kind = %(kind)s AND active = TRUE
        ORDER BY name;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"user_id": user_id, "kind": kind})
            names = [row["name"] for row in cur.fetchall()]
    if names:
        return names
    return DEFAULT_INCOME_CATEGORIES if kind == "income" else DEFAULT_EXPENSE_CATEGORIES


def set_user_categories(
    user_id: int,
    kind: str,
    names: list[str],
    source: str,
    renames: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Make `names` the user's active categories of `kind`, in one transaction.

    Renames run first and keep the category row (records follow it by id). Then names
    that exist are reactivated, new ones are added, and active ones not in `names` are
    deactivated — never deleted, so past records keep their category. Every change is
    logged in category_events. Names are matched case-insensitively. Returns the active
    names.
    """
    log_event = """
        INSERT INTO category_events
            (user_id, category_id, kind, action, old_name, new_name, source)
        VALUES (%(user_id)s, %(category_id)s, %(kind)s, %(action)s,
                %(old_name)s, %(new_name)s, %(source)s);
    """
    base = {"user_id": user_id, "kind": kind, "source": source}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, active FROM categories
                WHERE user_id = %(user_id)s AND kind = %(kind)s
                FOR UPDATE;
                """,
                base,
            )
            existing = {row["name"].lower(): dict(row) for row in cur.fetchall()}

            for old, new in renames or []:
                row = existing.get(old.lower())
                if row is None or new.lower() in existing:
                    continue  # unknown source, or the target exists (that's a merge)
                cur.execute(
                    "UPDATE categories SET name = %(new)s WHERE id = %(id)s;",
                    {"new": new, "id": row["id"]},
                )
                cur.execute(log_event, {**base, "category_id": row["id"], "action": "rename",
                                        "old_name": row["name"], "new_name": new})
                existing[new.lower()] = {**existing.pop(old.lower()), "name": new}

            wanted = {name.lower(): name for name in names}
            for key, name in wanted.items():
                row = existing.get(key)
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO categories (user_id, name, kind)
                        VALUES (%(user_id)s, %(name)s, %(kind)s) RETURNING id;
                        """,
                        {**base, "name": name},
                    )
                    category_id = cur.fetchone()["id"]
                    cur.execute(log_event, {**base, "category_id": category_id, "action": "add",
                                            "old_name": None, "new_name": name})
                elif not row["active"]:
                    cur.execute(
                        "UPDATE categories SET active = TRUE WHERE id = %(id)s;", {"id": row["id"]}
                    )
                    cur.execute(log_event, {**base, "category_id": row["id"], "action": "reactivate",
                                            "old_name": None, "new_name": row["name"]})

            for key, row in existing.items():
                if row["active"] and key not in wanted:
                    cur.execute(
                        "UPDATE categories SET active = FALSE WHERE id = %(id)s;", {"id": row["id"]}
                    )
                    cur.execute(log_event, {**base, "category_id": row["id"], "action": "deactivate",
                                            "old_name": row["name"], "new_name": None})
            conn.commit()
    logger.info(
        "Set %d %s categories for user=%d (source=%s)", len(wanted), kind, user_id, source
    )
    return [existing.get(key, {}).get("name", name) for key, name in wanted.items()]


def get_category_overview(user_id: int) -> list[dict]:
    """Every category of the user (active or not) with its number of live records —
    what the users agent shows and uses to decide whether a change needs confirmation."""
    query = """
        SELECT c.id, c.name, c.kind, c.active,
               (SELECT COUNT(*) FROM expenses e
                 WHERE e.category_id = c.id AND e.deleted_at IS NULL)
             + (SELECT COUNT(*) FROM incomes i
                 WHERE i.category_id = c.id AND i.deleted_at IS NULL) AS records
        FROM categories c
        WHERE c.user_id = %(user_id)s
        ORDER BY c.kind, c.active DESC, c.name;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"user_id": user_id})
            return [dict(row) for row in cur.fetchall()]


_CATEGORY_EVENT = """
    INSERT INTO category_events
        (user_id, category_id, kind, action, old_name, new_name, records_affected, source)
    VALUES (%(user_id)s, %(category_id)s, %(kind)s, %(action)s,
            %(old_name)s, %(new_name)s, %(records)s, %(source)s);
"""


def change_category(
    user_id: int,
    kind: str,
    action: str,
    name: str,
    new_name: str | None = None,
    source: str = "users_agent",
) -> dict:
    """Apply one category change in a transaction and log it in category_events.

    action: "add" (reactivates a deactivated name), "rename" (onto an existing name it
    becomes a merge: the records are re-pointed to the target and the source is
    deactivated), or "deactivate" (never deleted; past records keep it). 'otro' is the
    fallback category and can't be renamed or deactivated.

    Returns {"status": ..., "name": ..., "records": n}. status is one of added,
    reactivated, exists, renamed, merged, deactivated, already_inactive, not_found,
    protected.
    """
    name = " ".join(name.lower().split())[:50]
    new_name = " ".join((new_name or "").lower().split())[:50] or None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, active FROM categories
                WHERE user_id = %(user_id)s AND kind = %(kind)s
                FOR UPDATE;
                """,
                {"user_id": user_id, "kind": kind},
            )
            rows = {row["name"].lower(): dict(row) for row in cur.fetchall()}
            row = rows.get(name)

            def log(category_id, event, old=None, new=None, records=None):
                cur.execute(_CATEGORY_EVENT, {
                    "user_id": user_id, "category_id": category_id, "kind": kind,
                    "action": event, "old_name": old, "new_name": new,
                    "records": records, "source": source,
                })

            if action == "add":
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO categories (user_id, name, kind)
                        VALUES (%(user_id)s, %(name)s, %(kind)s) RETURNING id;
                        """,
                        {"user_id": user_id, "name": name, "kind": kind},
                    )
                    log(cur.fetchone()["id"], "add", new=name)
                    result = {"status": "added", "name": name}
                elif not row["active"]:
                    cur.execute("UPDATE categories SET active = TRUE WHERE id = %(id)s;", row)
                    log(row["id"], "reactivate", new=row["name"])
                    result = {"status": "reactivated", "name": row["name"]}
                else:
                    result = {"status": "exists", "name": row["name"]}

            elif row is None:
                result = {"status": "not_found", "name": name}
            elif row["name"].lower() == "otro":
                result = {"status": "protected", "name": row["name"]}

            elif action == "rename":
                target = rows.get(new_name or "")
                if not new_name or target is row:
                    result = {"status": "exists", "name": row["name"]}
                elif target is None:
                    cur.execute(
                        "UPDATE categories SET name = %(new)s WHERE id = %(id)s;",
                        {"new": new_name, "id": row["id"]},
                    )
                    log(row["id"], "rename", old=row["name"], new=new_name)
                    result = {"status": "renamed", "name": new_name}
                else:
                    moved = 0
                    for table in ("expenses", "incomes"):
                        cur.execute(
                            f"UPDATE {table} SET category_id = %(target)s "
                            "WHERE user_id = %(user_id)s AND category_id = %(source)s;",
                            {"target": target["id"], "source": row["id"], "user_id": user_id},
                        )
                        moved += cur.rowcount or 0
                    cur.execute("UPDATE categories SET active = FALSE WHERE id = %(id)s;", row)
                    if not target["active"]:
                        cur.execute("UPDATE categories SET active = TRUE WHERE id = %(id)s;", target)
                    log(row["id"], "merge", old=row["name"], new=target["name"], records=moved)
                    result = {"status": "merged", "name": target["name"], "records": moved}

            elif action == "deactivate":
                if not row["active"]:
                    result = {"status": "already_inactive", "name": row["name"]}
                else:
                    cur.execute("UPDATE categories SET active = FALSE WHERE id = %(id)s;", row)
                    log(row["id"], "deactivate", old=row["name"])
                    result = {"status": "deactivated", "name": row["name"]}
            else:
                raise ValueError(f"unknown category action {action!r}")
            conn.commit()
    logger.info(
        "Category %s (%s) for user=%d → %s", action, kind, user_id, result["status"]
    )
    return result


# ── Preferences (append-only history) ────────────────────────────────────────────
#
# user_prefs is never updated in place: every change inserts a new row holding the full
# preferences snapshot, and the current preferences are the user's latest row. The old
# users.prefs column is no longer read.


def get_user_prefs(user_id: int) -> dict:
    """Return the user's current preferences — the latest user_prefs row (profile, and
    later currency / report settings). Empty dict when the user has never set any."""
    query = """
        SELECT prefs FROM user_prefs
        WHERE user_id = %(user_id)s
        ORDER BY created_at DESC, id DESC
        LIMIT 1;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"user_id": user_id})
            row = cur.fetchone()
    return (row["prefs"] if row else None) or {}


def append_user_prefs(
    user_id: int, changes: dict, source: str = "manual", replace: bool = False
) -> int:
    """Record a preferences change as a new user_prefs row and return its id.

    The new row's snapshot is the latest preferences with `changes` merged on top
    (top-level keys replaced), read and written in one transaction so two concurrent
    changes can't drop each other's keys. With replace=True the snapshot is exactly
    `changes` (a completed onboarding writes a complete new configuration).
    """
    latest = """
        SELECT prefs FROM user_prefs
        WHERE user_id = %(user_id)s
        ORDER BY created_at DESC, id DESC
        LIMIT 1;
    """
    insert = """
        INSERT INTO user_prefs (user_id, prefs, changes, source)
        VALUES (%(user_id)s, %(prefs)s, %(changes)s, %(source)s)
        RETURNING id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Lock the user row so concurrent appends for the same user serialize
            cur.execute("SELECT id FROM users WHERE id = %(user_id)s FOR UPDATE;", {"user_id": user_id})
            cur.execute(latest, {"user_id": user_id})
            row = cur.fetchone()
            previous = {} if replace else ((row["prefs"] if row else None) or {})
            prefs = {**previous, **changes}
            cur.execute(
                insert,
                {
                    "user_id": user_id,
                    "prefs": Json(prefs),
                    "changes": Json(changes),
                    "source": source,
                },
            )
            prefs_id = cur.fetchone()["id"]
            conn.commit()
    logger.info(
        "Appended prefs id=%d for user=%d (source=%s, keys=%s)",
        prefs_id, user_id, source, sorted(changes),
    )
    return prefs_id


# ── Conversation memory ──────────────────────────────────────────────────────────

def save_turn(
    user_id: int,
    role: str,
    content: str,
    external_id: str | None = None,
    message_id: int | None = None,
) -> None:
    """Append a turn to the conversation history. role is 'user' or 'assistant'.

    external_id is the Twilio SID of an outbound message (so a swipe-reply to it can be
    resolved); message_id is the inbound message the turn is (user) or answers
    (assistant), which links a bot confirmation to the records that message created.
    """
    query = """
        INSERT INTO conversation_turns (user_id, role, content, external_id, message_id)
        VALUES (%(user_id)s, %(role)s, %(content)s, %(external_id)s, %(message_id)s)
        ON CONFLICT (external_id) DO NOTHING;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "user_id": user_id,
                "role": role,
                "content": content,
                "external_id": external_id,
                "message_id": message_id,
            })
            conn.commit()


# ── Reply context (WhatsApp swipe-replies) ───────────────────────────────────────


def find_replied_message(user_id: int, sid: str) -> dict | None:
    """Find a quoted message by its Twilio SID, only among this user's messages.

    The user's own messages live in `messages` (by whatsapp_message_id); the bot's in
    `conversation_turns` (by external_id). Returns {role, text, created_at, message_id}
    or None when it isn't stored.
    """
    params = {"user_id": user_id, "sid": sid}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS message_id, raw_text, transcript, created_at
                FROM messages
                WHERE user_id = %(user_id)s AND whatsapp_message_id = %(sid)s;
                """,
                params,
            )
            row = cur.fetchone()
            if row is not None:
                return {
                    "role": "user",
                    "text": row["transcript"] or row["raw_text"] or "",
                    "created_at": row["created_at"],
                    "message_id": row["message_id"],
                }
            cur.execute(
                """
                SELECT content, created_at, message_id FROM conversation_turns
                WHERE user_id = %(user_id)s AND external_id = %(sid)s;
                """,
                params,
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "role": "assistant",
        "text": row["content"],
        "created_at": row["created_at"],
        "message_id": row["message_id"],
    }


def get_message_records(user_id: int, message_id: int) -> list[dict]:
    """The live expenses and incomes created from one inbound message, oldest first."""
    query = """
        SELECT 'expense' AS kind, e.id, e.amount, e.currency, c.name AS category,
               e.expense_date AS date, e.description
        FROM expenses e JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = %(user_id)s AND e.message_id = %(message_id)s
          AND e.deleted_at IS NULL
        UNION ALL
        SELECT 'income' AS kind, i.id, i.amount, i.currency, c.name AS category,
               i.income_date AS date, i.description
        FROM incomes i JOIN categories c ON c.id = i.category_id
        WHERE i.user_id = %(user_id)s AND i.message_id = %(message_id)s
          AND i.deleted_at IS NULL
        ORDER BY kind, id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"user_id": user_id, "message_id": message_id})
            return [dict(row) for row in cur.fetchall()]


def load_recent_turns(
    user_id: int,
    max_turns: int = CONVERSATION_MAX_TURNS,
    window_minutes: int = CONVERSATION_WINDOW_MINUTES,
) -> list[dict]:
    """Return recent conversation turns (chronological order) within the time window,
    capped at max_turns. Used to give the chatbot short-term memory."""
    query = """
        SELECT role, content FROM conversation_turns
        WHERE user_id = %(user_id)s
          AND created_at >= NOW() - (%(window_minutes)s * INTERVAL '1 minute')
        ORDER BY created_at DESC
        LIMIT %(max_turns)s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "user_id": user_id,
                "window_minutes": window_minutes,
                "max_turns": max_turns,
            })
            rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()  # chronological (oldest first) for the LLM
    return rows


# ── Messages & expenses ──────────────────────────────────────────────────────────

def save_message(user_id: int, whatsapp_message_id: str, phone_number: str, raw_text: str) -> int | None:
    """Insert a raw WhatsApp message. Returns the new row ID, or None on duplicate."""
    query = """
        INSERT INTO messages (user_id, whatsapp_message_id, phone_number, raw_text)
        VALUES (%(user_id)s, %(whatsapp_message_id)s, %(phone_number)s, %(raw_text)s)
        ON CONFLICT (whatsapp_message_id) DO NOTHING
        RETURNING id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "user_id": user_id,
                "whatsapp_message_id": whatsapp_message_id,
                "phone_number": phone_number,
                "raw_text": raw_text,
            })
            row = cur.fetchone()
            if row is None:
                logger.debug("Duplicate message sid=%s, skipping insert", whatsapp_message_id)
                return None
            conn.commit()
            logger.debug("Inserted message id=%d sid=%s", row["id"], whatsapp_message_id)
            return row["id"]


def save_expense(message_id: int, expense: dict, user_id: int) -> int:
    """Insert a parsed expense linked to a message and user. Returns the new row ID.

    The category name is resolved to the user's category_id (unknown → 'otro'), and
    expense["category"] is set to the stored name so the confirmation matches.
    """
    query = """
        INSERT INTO expenses (
            user_id, message_id, amount, currency, category_id, expense_date,
            payment_method, merchant, description, confidence, source
        ) VALUES (
            %(user_id)s, %(message_id)s, %(amount)s, %(currency)s, %(category_id)s, %(date)s,
            %(payment_method)s, %(merchant)s, %(description)s, %(confidence)s, %(source)s
        )
        RETURNING id;
    """
    expense["user_id"] = user_id
    expense["message_id"] = message_id
    expense.setdefault("currency", "COP")
    expense.setdefault("source", "text")
    with get_connection() as conn:
        with conn.cursor() as cur:
            category = _resolve_category(cur, user_id, "expense", expense.get("category"))
            expense["category_id"] = category["id"]
            expense["category"] = category["name"]
            cur.execute(query, expense)
            row_id = cur.fetchone()["id"]
            conn.commit()
            logger.debug("Inserted expense id=%d message_id=%d", row_id, message_id)
            return row_id


def save_income(message_id: int, income: dict, user_id: int) -> int:
    """Insert a parsed income record linked to a message and user. Returns the new row ID.

    Mirrors save_expense's pattern exactly, but `source` here means the money's origin
    ('manual' | 'gmail' | 'bank'), not the WhatsApp message channel — callers must not
    pass a message_type value into it. The category is resolved to an id like there.
    """
    query = """
        INSERT INTO incomes (
            user_id, message_id, amount, currency, category_id, income_date,
            description, confidence, source
        ) VALUES (
            %(user_id)s, %(message_id)s, %(amount)s, %(currency)s, %(category_id)s, %(date)s,
            %(description)s, %(confidence)s, %(source)s
        )
        RETURNING id;
    """
    income["user_id"] = user_id
    income["message_id"] = message_id
    income.setdefault("currency", "COP")
    income.setdefault("source", "manual")
    with get_connection() as conn:
        with conn.cursor() as cur:
            category = _resolve_category(cur, user_id, "income", income.get("category"))
            income["category_id"] = category["id"]
            income["category"] = category["name"]
            cur.execute(query, income)
            row_id = cur.fetchone()["id"]
            conn.commit()
            logger.debug("Inserted income id=%d message_id=%d", row_id, message_id)
            return row_id


def get_expenses(user_id: int, min_date: str, max_date: str) -> list[dict]:
    """Return all non-deleted expenses for a user within the date range (inclusive).
    `category` is the category's current name, so renames show up in past reports."""
    query = """
        SELECT e.amount, e.currency, c.name AS category, e.expense_date,
               e.payment_method, e.merchant, e.description, e.source
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = %(user_id)s
          AND e.deleted_at IS NULL
          AND e.expense_date BETWEEN %(min_date)s AND %(max_date)s
        ORDER BY e.expense_date, c.name;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "user_id": user_id,
                "min_date": min_date,
                "max_date": max_date,
            })
            rows = cur.fetchall()
            logger.debug(
                "Fetched %d expenses for user=%d between %s and %s",
                len(rows), user_id, min_date, max_date,
            )
            return [dict(row) for row in rows]


def update_message_transcript(message_id: int, transcript: str) -> None:
    """Persist a Whisper transcript on the messages row."""
    query = "UPDATE messages SET transcript = %(transcript)s WHERE id = %(id)s;"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"transcript": transcript, "id": message_id})
            conn.commit()
            logger.debug("Updated transcript for message id=%d", message_id)
