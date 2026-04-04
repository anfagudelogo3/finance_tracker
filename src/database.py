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
            message_id, amount, category, expense_date,
            payment_method, merchant, description, confidence
        ) VALUES (
            %(message_id)s, %(amount)s, %(category)s, %(date)s,
            %(payment_method)s, %(merchant)s, %(description)s, %(confidence)s
        )
        RETURNING id;
    """
    expense["message_id"] = message_id
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, expense)
            row_id = cur.fetchone()["id"]
            conn.commit()
            logger.debug("Inserted expense id=%d message_id=%d", row_id, message_id)
            return row_id
