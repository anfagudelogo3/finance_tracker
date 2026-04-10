import logging

import boto3
import requests

from config import S3_BUCKET_NAME, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN

logger = logging.getLogger(__name__)

_s3 = boto3.client("s3")


def _build_s3_key(phone: str, message_sid: str, s3_prefix: str, ext: str) -> str:
    """Build the S3 key following the agreed bucket structure.

    Pattern: incoming/whatsapp/{s3_prefix}/{phone}/{message_sid}.{ext}
    Example: incoming/whatsapp/audio/573014027381/MMf64926523895851d.ogg
    """
    return f"incoming/whatsapp/{s3_prefix}/{phone}/{message_sid}.{ext}"


def download_and_store(
    phone: str,
    message_sid: str,
    media_item: dict,
) -> tuple[str, bytes]:
    """Download a Twilio media file, upload it to S3, and return (s3_key, bytes).

    Args:
        phone: Sender phone number (without 'whatsapp:' prefix).
        message_sid: Twilio MessageSid — used as the S3 filename.
        media_item: Dict with 'url', 'content_type', 's3_prefix', 'ext'
                    as produced by webhook.extract_message.

    Returns:
        Tuple of (s3_key, raw_bytes) so the caller can pass bytes to an LLM
        without a second download.

    Raises:
        requests.HTTPError: If the Twilio media download fails.
        boto3 exceptions: If the S3 upload fails.
    """
    url = media_item["url"]
    content_type = media_item["content_type"]
    s3_prefix = media_item["s3_prefix"]
    ext = media_item["ext"]

    logger.info(
        "Downloading media: type=%s sid=%s from=%s",
        content_type, message_sid, phone,
    )

    # Twilio media URLs require Basic Auth (Account SID : Auth Token)
    response = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
    response.raise_for_status()

    media_bytes = response.content
    logger.info("Downloaded %d bytes for sid=%s", len(media_bytes), message_sid)

    s3_key = _build_s3_key(phone, message_sid, s3_prefix, ext)

    _s3.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_key,
        Body=media_bytes,
        ContentType=content_type,
    )
    logger.info("Uploaded to S3: bucket=%s key=%s", S3_BUCKET_NAME, s3_key)

    return s3_key, media_bytes


def store_all_media(phone: str, message_sid: str, media: list[dict]) -> list[dict]:
    """Download and store all media items. Returns list of dicts with s3_key, bytes, content_type, s3_prefix."""
    results = []
    for item in media:
        s3_key, media_bytes = download_and_store(phone, message_sid, item)
        results.append({
            "s3_key": s3_key,
            "bytes": media_bytes,
            "content_type": item["content_type"],
            "s3_prefix": item["s3_prefix"],
            "ext": item["ext"],
        })
    return results
