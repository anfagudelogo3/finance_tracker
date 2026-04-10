import logging

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL

logger = logging.getLogger(__name__)


def get_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def save_message(whatsapp_message_id: str, phone_number: str, raw_text: str) -> int:
    """Insert a raw WhatsApp message. Returns the new row ID."""
    query = """
        INSERT INTO messages (whatsapp_message_id, phone_number, raw_text)
        VALUES (%(whatsapp_message_id)s, %(phone_number)s, %(raw_text)s)
        RETURNING id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "whatsapp_message_id": whatsapp_message_id,
                "phone_number": phone_number,
                "raw_text": raw_text,
            })
            row_id = cur.fetchone()["id"]
            conn.commit()
            logger.debug("Inserted message id=%d sid=%s", row_id, whatsapp_message_id)
            return row_id


def save_expense(message_id: int, expense: dict) -> int:
    """Insert a parsed expense linked to a message. Returns the new row ID."""
    query = """
        INSERT INTO expenses (
            message_id, amount, currency, category, expense_date,
            payment_method, merchant, description, confidence, source
        ) VALUES (
            %(message_id)s, %(amount)s, %(currency)s, %(category)s, %(date)s,
            %(payment_method)s, %(merchant)s, %(description)s, %(confidence)s, %(source)s
        )
        RETURNING id;
    """
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


def get_expenses(phone_number: str, min_date: str, max_date: str) -> list[dict]:
    """Return all expenses for a user within the given date range (inclusive)."""
    query = """
        SELECT e.amount, e.currency, e.category, e.expense_date,
               e.payment_method, e.merchant, e.description, e.source
        FROM expenses e
        JOIN messages m ON e.message_id = m.id
        WHERE m.phone_number = %(phone_number)s
          AND e.expense_date BETWEEN %(min_date)s AND %(max_date)s
        ORDER BY e.expense_date, e.category;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {
                "phone_number": phone_number,
                "min_date": min_date,
                "max_date": max_date,
            })
            rows = cur.fetchall()
            logger.debug(
                "Fetched %d expenses for %s between %s and %s",
                len(rows), phone_number, min_date, max_date,
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
