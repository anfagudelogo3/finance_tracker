import base64
import json
import logging
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

# Tracing is set up before the OpenAI parser / orchestrator are imported so OpenAI and
# Claude calls are instrumented (both client singletons are created at import time).
from finance_tracker.observability.tracing import setup_tracing
setup_tracing()

from finance_tracker.config import (
    ALLOWED_PHONE_NUMBERS,
    WEBHOOK_URL,
    MSG_ERROR,
    MSG_EMPTY_EXPENSE,
    DEFAULT_PROFILE,
    DEFAULT_CURRENCY,
    TIMEZONE,
    REPLY_RESERVE_SECONDS,
)
from finance_tracker.core import deadline
from finance_tracker.channels.whatsapp.webhook import verify_signature, extract_message
from finance_tracker.legacy.openai_parser import (
    parse_expense_from_image,
    transcribe_audio,
    parse_report_request
)
from finance_tracker.routing.fast_paths import is_excel_request, fast_route
from finance_tracker.core.agent_types import AgentRequest
from finance_tracker.routing import orchestrator
from finance_tracker.agents import onboarding, pending
from finance_tracker.channels.whatsapp import reply_context
from finance_tracker.storage.excel import create_excel_bytes, upload_and_sign
from finance_tracker.storage.database import (
    save_message,
    save_expense,
    get_expenses,
    get_incomes,
    update_message_transcript,
    get_or_create_user,
    get_user_categories,
    save_turn,
    load_recent_turns,
    get_user_prefs,
)
from finance_tracker.storage.media import store_all_media
from finance_tracker.channels.whatsapp.sender import send_message, send_document, format_confirmation

logger = logging.getLogger(__name__)
# The whole package, not just this module: Lambda's root logger is at WARNING, which
# hid the agents' per-call timing and token logs
logging.getLogger("finance_tracker").setLevel(logging.INFO)


def handler(event, context):
    """AWS Lambda entry point for the Twilio WhatsApp webhook."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    logger.info("Incoming request", extra={"method": method})

    # One time budget for everything this message does, ending early enough to still
    # send a reply: a Lambda that hits its own timeout dies without answering
    if context is not None:
        deadline.start(
            context.get_remaining_time_in_millis() / 1000 - REPLY_RESERVE_SECONDS
        )
    else:
        deadline.clear()

    if method == "POST":
        return _handle_message(event)

    logger.warning("Unsupported HTTP method: %s", method)
    return {"statusCode": 405, "body": "Method Not Allowed"}


def _build_url(event: dict) -> str:
    """Return the webhook URL for Twilio signature validation.

    Uses the WEBHOOK_URL env var (set to the exact URL configured in Twilio)
    to avoid any mismatch from reconstructing the URL from the Lambda event.
    """
    return WEBHOOK_URL


def _get_message_type(message: dict) -> str:
    """Return 'audio', 'image', or 'text' based on the first media item."""
    if not message["media"]:
        return "text"
    s3_prefix = message["media"][0]["s3_prefix"]
    if s3_prefix == "audio":
        return "audio"
    if s3_prefix == "images":
        return "image"
    return "text"


def _parse_form_body(event: dict) -> dict:
    """Parse a URL-encoded form body from a Lambda event into a flat dict."""
    body_str = event.get("body", "")
    if event.get("isBase64Encoded", False):
        body_str = base64.b64decode(body_str).decode("utf-8")
    parsed = parse_qs(body_str, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _send(user_id: int, phone: str, text: str, message_id: int | None) -> None:
    """Send one reply and record it as an assistant turn with its Twilio SID and the
    inbound message it answers — what lets a later swipe-reply to it be resolved, down
    to the records that message created."""
    message_sid = send_message(phone, text)
    logger.info("Reply sent, Twilio SID: %s", message_sid)
    save_turn(user_id, "assistant", text, external_id=message_sid, message_id=message_id)


def _dispatch_and_reply(
    user_id, phone, msg_type, agent_text, message_id, now, conversation, prefs,
    replied_to=None,
):
    """Build an AgentRequest, dispatch through the orchestrator, and send the reply.

    Shared by the report branch and the general expense/income branch — both just
    differ in what text/message_type they hand the orchestrator.
    """
    agent_request = AgentRequest(
        user_id=user_id,
        message_id=message_id,
        phone=phone,
        text=agent_text,
        media=[],
        message_type=msg_type,
        now=now,
        conversation=conversation,
        profile=prefs.get("profile") or dict(DEFAULT_PROFILE),
        default_currency=prefs.get("default_currency") or DEFAULT_CURRENCY,
        replied_to=replied_to,
    )
    agent_response = orchestrator.handle_message(agent_request)
    _send(user_id, phone, agent_response.reply_text, message_id)
    return {"statusCode": 200, "body": ""}


def _route(
    user_id, phone, msg_type, text, message_id, now, conversation, prefs,
    stored_media=(), caption="", replied_to=None,
):
    """Intent routing for one message (first match wins). Also used to replay the
    message held during onboarding, with that message's own id and timestamp.
    `replied_to` is the quoted message of a WhatsApp swipe-reply (or None)."""
    profile = prefs.get("profile") or dict(DEFAULT_PROFILE)

    # Excel export branch
    if is_excel_request(text):
        logger.info("Excel export request detected from %s", phone)
        date_range = parse_report_request(text, now)
        logger.info(
            "Excel date range: %s to %s",
            date_range["min_date"], date_range["max_date"],
        )
        expenses = get_expenses(user_id, date_range["min_date"], date_range["max_date"])
        incomes = get_incomes(user_id, date_range["min_date"], date_range["max_date"])
        logger.info(
            "Fetched %d expense(s) and %d income(s) for Excel export",
            len(expenses), len(incomes),
        )
        excel_bytes = create_excel_bytes(expenses, incomes)
        filename = f"movimientos_{date_range['min_date']}_{date_range['max_date']}.xlsx"
        presigned_url = upload_and_sign(
            excel_bytes, phone, date_range["min_date"], date_range["max_date"]
        )
        message_sid = send_document(phone, presigned_url, filename)
        logger.info("Excel sent, Twilio SID: %s", message_sid)
        save_turn(
            user_id, "assistant", f"[excel] {filename}",
            external_id=message_sid, message_id=message_id,
        )
        return {"statusCode": 200, "body": ""}

    # Report branch — only the narrowed fast path (personal profile, single intent);
    # anything else that looks like a report reaches the router via the orchestrator
    if fast_route(text, profile, conversation, replied_to) == "reporting":
        logger.info("Report request detected from %s", phone)
        return _dispatch_and_reply(
            user_id, phone, msg_type, text, message_id, now, conversation, prefs
        )

    # Image expense branch — unchanged, still the old OpenAI vision path
    if msg_type == "image":
        expense_categories = get_user_categories(user_id, kind="expense")
        media_item = stored_media[0]
        expenses = parse_expense_from_image(
            media_item["bytes"],
            media_item["content_type"],
            caption=caption,
            categories=expense_categories,
        )
        logger.info("Parsed %d expense(s) from message id=%d (source=image)", len(expenses), message_id)

        if not expenses:
            logger.info("No expenses parsed from message id=%d, sending fallback reply", message_id)
            _send(user_id, phone, MSG_EMPTY_EXPENSE, message_id)
            return {"statusCode": 200, "body": ""}

        for expense in expenses:
            expense["source"] = "image"
            expense_id = save_expense(message_id, expense, user_id, event_source="image_parser")
            logger.info(
                "Expense saved: id=%d amount=%s category=%s confidence=%s source=image",
                expense_id, expense.get("amount"), expense.get("category"), expense.get("confidence"),
            )

        _send(user_id, phone, format_confirmation(expenses), message_id)
        return {"statusCode": 200, "body": ""}

    # Text / audio expense/income branch — dispatched via the orchestrator
    return _dispatch_and_reply(
        user_id, phone, msg_type, text, message_id, now, conversation, prefs,
        replied_to=replied_to,
    )


def _handle_message(event):
    """Process an incoming WhatsApp message from Twilio."""
    params = _parse_form_body(event)
    # Field names only: the values include the sender's phone, name and message text
    logger.info("Webhook fields: %s", sorted(params))

    # Verify webhook signature
    url = _build_url(event)
    signature = (event.get("headers", {}) or {}).get("x-twilio-signature", "")
    logger.info("Validating signature | url=%s | signature_present=%s | param_count=%d", url, bool(signature), len(params))
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

    # Save raw message and process — wrapped so any failure sends a friendly reply
    try:
        # Resolve the user from the verified phone (the LLM never chooses this)
        user_id = get_or_create_user(message["phone"])

        message_id = save_message(
            user_id=user_id,
            whatsapp_message_id=message["message_id"],
            phone_number=message["phone"],
            raw_text=message["text"],
        )
        if message_id is None:
            logger.info("Duplicate message %s — already processed, skipping", message["message_id"])
            return {"statusCode": 200, "body": ""}
        logger.info("Message saved with id=%d for user=%d", message_id, user_id)

        # Load conversation history BEFORE recording this message's own turn, so it
        # naturally excludes the message currently being processed.
        conversation = load_recent_turns(user_id)

        # Latest preferences: profile (drives routing), currency, onboarding state
        prefs = get_user_prefs(user_id)

        # Record the inbound turn for short-term conversation memory
        save_turn(
            user_id, "user", message["text"] or f"[{_get_message_type(message)}]",
            message_id=message_id,
        )

        # Store any media attachments to S3 and keep bytes for LLM processing
        stored_media = []
        if message["media"]:
            stored_media = store_all_media(message["phone"], message["message_id"], message["media"])
            logger.info("Stored %d media file(s) to S3", len(stored_media))

        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        msg_type = _get_message_type(message)
        logger.info("Message type: %s", msg_type)

        # Voice notes are transcribed first: onboarding answers can be voice notes too
        if msg_type == "audio":
            media_item = stored_media[0]
            ext = media_item["ext"]
            transcript = transcribe_audio(media_item["bytes"], filename=f"audio.{ext}")
            update_message_transcript(message_id, transcript)
            text = f"{transcript} {message['text']}".strip()
        else:
            text = message["text"]

        # Onboarding blocks everything else until it's done or skipped (Phase 6b)
        outcome = onboarding.handle(user_id, prefs, text, msg_type, message_id, now)
        if outcome.handled:
            for reply in outcome.replies:
                _send(user_id, message["phone"], reply, message_id)
            if outcome.replay is None:
                return {"statusCode": 200, "body": ""}
            held = outcome.replay
            return _route(
                user_id, message["phone"], held["message_type"], held["text"],
                held["message_id"], held["now"], [], outcome.prefs,
            )

        # A settings or record change waiting for "sí" is settled before any routing
        reply, prefs = pending.resolve(user_id, outcome.prefs, text, now, message_id)
        if reply:
            _send(user_id, message["phone"], reply, message_id)
            return {"statusCode": 200, "body": ""}

        # WhatsApp swipe-reply: what the quoted message was, and what it recorded (Phase 7a).
        # Onboarding and pending confirmations above ignore it on purpose.
        replied_to = reply_context.resolve(
            user_id, message["phone"], message.get("replied_to_sid")
        )
        if replied_to:
            logger.info(
                "Reply to a %s message (source=%s, %d record(s))",
                replied_to["role"], replied_to["source"], len(replied_to["records"]),
            )

        return _route(
            user_id, message["phone"], msg_type, text, message_id, now, conversation,
            prefs, stored_media=stored_media, caption=message["text"],
            replied_to=replied_to,
        )

    except Exception:
        logger.exception("Unhandled error processing message from %s", message["phone"])
        try:
            error_sid = send_message(message["phone"], MSG_ERROR)
            if "user_id" in locals():
                save_turn(user_id, "assistant", MSG_ERROR, external_id=error_sid)
        except Exception:
            logger.exception("Failed to send error message to %s", message["phone"])
        return {"statusCode": 200, "body": ""}