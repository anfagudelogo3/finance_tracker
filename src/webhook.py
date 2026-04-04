from twilio.request_validator import RequestValidator

from config import TWILIO_AUTH_TOKEN


def verify_signature(url: str, params: dict, signature: str) -> bool:
    """Verify that the webhook request was sent by Twilio."""
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)


def extract_message(params: dict) -> dict | None:
    """Extract sender phone number and message text from a Twilio webhook payload.

    Twilio sends form-encoded fields: From (whatsapp:+57...), Body, MessageSid.
    Returns a dict with 'phone', 'text', and 'message_id', or None if incomplete.
    """
    from_number = params.get("From", "")
    body = params.get("Body", "")
    message_sid = params.get("MessageSid", "")

    if not from_number or not body or not message_sid:
        return None

    return {
        "phone": from_number.removeprefix("whatsapp:"),
        "text": body.strip(),
        "message_id": message_sid,
    }
