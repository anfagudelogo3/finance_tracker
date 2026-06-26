import logging
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from config import (
    DATABASE_URL,
    DEFAULT_EXPENSE_CATEGORIES,
    DEFAULT_INCOME_CATEGORIES,
    CONVERSATION_MAX_TURNS,
    CONVERSATION_WINDOW_MINUTES,
)

logger = logging.getLogger(__name__)


@contextmanager
def get_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
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
    logger.debug("Seeded %d default categories for user id=%d", len(rows), user_id)


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


# ── Conversation memory ──────────────────────────────────────────────────────────

def save_turn(user_id: int, role: str, content: str) -> None:
    """Append a turn to the conversation history. role is 'user' or 'assistant'."""
    query = """
        INSERT INTO conversation_turns (user_id, role, content)
        VALUES (%(user_id)s, %(role)s, %(content)s);
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"user_id": user_id, "role": role, "content": content})
            conn.commit()


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
    """Insert a parsed expense linked to a message and user. Returns the new row ID."""
    query = """
        INSERT INTO expenses (
            user_id, message_id, amount, currency, category, expense_date,
            payment_method, merchant, description, confidence, source
        ) VALUES (
            %(user_id)s, %(message_id)s, %(amount)s, %(currency)s, %(category)s, %(date)s,
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
            cur.execute(query, expense)
            row_id = cur.fetchone()["id"]
            conn.commit()
            logger.debug("Inserted expense id=%d message_id=%d", row_id, message_id)
            return row_id


def get_expenses(user_id: int, min_date: str, max_date: str) -> list[dict]:
    """Return all non-deleted expenses for a user within the date range (inclusive)."""
    query = """
        SELECT amount, currency, category, expense_date,
               payment_method, merchant, description, source
        FROM expenses
        WHERE user_id = %(user_id)s
          AND deleted_at IS NULL
          AND expense_date BETWEEN %(min_date)s AND %(max_date)s
        ORDER BY expense_date, category;
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
