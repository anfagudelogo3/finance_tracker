import base64
import json
import logging
from urllib.parse import parse_qs

from config import ALLOWED_PHONE_NUMBERS
from webhook import verify_signature, extract_message
from parser import parse_expense
from database import save_message, save_expense
from whatsapp import send_message, format_confirmation

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """AWS Lambda entry point for the Twilio WhatsApp webhook."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    logger.info("Incoming request", extra={"method": method})

    if method == "POST":
        return _handle_message(event)

    logger.warning("Unsupported HTTP method: %s", method)
    return {"statusCode": 405, "body": "Method Not Allowed"}


def _build_url(event: dict) -> str:
    """Reconstruct the full HTTPS request URL for Twilio signature validation."""
    domain = event.get("requestContext", {}).get("domainName", "")
    path = event.get("rawPath", "/")
    return f"https://{domain}{path}"


def _parse_form_body(event: dict) -> dict:
    """Parse a URL-encoded form body from a Lambda event into a flat dict."""
    body_str = event.get("body", "")
    if event.get("isBase64Encoded", False):
        body_str = base64.b64decode(body_str).decode("utf-8")
    parsed = parse_qs(body_str)
    return {k: v[0] for k, v in parsed.items()}


def _handle_message(event):
    """Process an incoming WhatsApp message from Twilio."""
    params = _parse_form_body(event)

    # Verify webhook signature
    url = _build_url(event)
    signature = (event.get("headers", {}) or {}).get("x-twilio-signature", "")
    if not verify_signature(url, params, signature):
        logger.warning("Invalid Twilio signature for URL: %s", url)
        return {"statusCode": 401, "body": "Invalid signature"}

    # Extract message
    message = extract_message(params)
    if not message:
        logger.info("Webhook received but no message extracted (status callback or non-text)")
        return {"statusCode": 200, "body": ""}

    logger.info("Message received from %s: %s", message["phone"], message["message_id"])

    # Verify sender
    if message["phone"] not in ALLOWED_PHONE_NUMBERS:
        logger.warning("Unauthorized sender: %s", message["phone"])
        return {"statusCode": 200, "body": ""}

    # Save raw message
    message_id = save_message(
        whatsapp_message_id=message["message_id"],
        phone_number=message["phone"],
        raw_text=message["text"],
    )
    logger.info("Message saved with id=%d", message_id)

    # Parse expense via LLM
    expense = parse_expense(message["text"])
    logger.info(
        "Expense parsed: amount=%s category=%s confidence=%s",
        expense.get("amount"),
        expense.get("category"),
        expense.get("confidence"),
    )

    # Save expense linked to message
    expense_id = save_expense(message_id, expense)
    logger.info("Expense saved with id=%d", expense_id)

    # Send confirmation
    confirmation = format_confirmation(expense)
    message_sid = send_message(message["phone"], confirmation)
    logger.info("Confirmation sent, Twilio SID: %s", message_sid)

    return {"statusCode": 200, "body": ""}