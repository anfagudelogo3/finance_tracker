from twilio.request_validator import RequestValidator

from config import TWILIO_AUTH_TOKEN

# MIME types we know how to handle, mapped to their S3 sub-prefix
_MEDIA_TYPE_PREFIX = {
    "image/jpeg": "images",
    "image/png": "images",
    "image/webp": "images",
    "audio/ogg": "audio",
    "audio/mpeg": "audio",
    "audio/mp4": "audio",
}

# Extension lookup for building S3 keys
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
}


def verify_signature(url: str, params: dict, signature: str) -> bool:
    """Verify that the webhook request was sent by Twilio."""
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)


def extract_message(params: dict) -> dict | None:
    """Extract sender, message text, and any media items from a Twilio webhook payload.

    Returns a dict with:
      - phone: normalized phone number (no 'whatsapp:' prefix)
      - text: message body (may be empty when media-only)
      - message_id: Twilio MessageSid
      - media: list of dicts with 'url', 'content_type', 's3_prefix', 'ext'
               (empty list when NumMedia == 0)

    Returns None if required fields are missing.
    """
    from_number = params.get("From", "")
    message_sid = params.get("MessageSid", "")

    if not from_number or not message_sid:
        return None

    body = params.get("Body", "").strip()
    num_media = int(params.get("NumMedia", "0"))

    media = []
    for i in range(num_media):
        url = params.get(f"MediaUrl{i}", "")
        content_type = params.get(f"MediaContentType{i}", "")
        if not url or not content_type:
            continue
        s3_prefix = _MEDIA_TYPE_PREFIX.get(content_type, "other")
        ext = _MIME_TO_EXT.get(content_type, "bin")
        media.append({
            "url": url,
            "content_type": content_type,
            "s3_prefix": s3_prefix,
            "ext": ext,
        })

    # Require at least a body or media to consider this a real message
    if not body and not media:
        return None

    return {
        "phone": from_number.removeprefix("whatsapp:"),
        "text": body,
        "message_id": message_sid,
        "media": media,
    }
