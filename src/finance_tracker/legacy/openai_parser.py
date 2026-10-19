"""OpenAI code paths that haven't migrated to Claude yet: image expenses (vision),
audio transcription (Whisper — permanent, Claude has no speech-to-text), Excel
date-range parsing, and the original text-expense parser (still evaluated for parity).
"""
import base64
import io
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from finance_tracker.config import (
    OPENAI_API_KEY,
    OPENAI_TEXT_MODEL,
    OPENAI_VISION_MODEL,
    OPENAI_AUDIO_MODEL,
    OPENAI_AUDIO_LANGUAGE,
    DEFAULT_EXPENSE_CATEGORIES,
)
from finance_tracker.core.confidence import estimate_confidence

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

_BOGOTA = ZoneInfo("America/Bogota")

def _build_system_prompt(categories: list[str] | None = None) -> str:
    """Build the expense-extraction system prompt with the user's categories injected.

    Categories are data (per-user, customizable) rather than hardcoded, so the prompt is
    built per request. Falls back to the defaults when none are supplied.
    """
    cats = ", ".join(categories) if categories else ", ".join(DEFAULT_EXPENSE_CATEGORIES)
    return f"""You are a personal finance assistant that extracts expense data from short text messages in Spanish or English.

Given a user message, extract ALL expenses mentioned and return ONLY a JSON object with a single key "expenses" containing an array. Each object in the array represents one expense. If only one expense is mentioned, return an array with one object.

Format:
{{"expenses": [{{...}}, {{...}}]}}

Each expense object has these fields:
{{
  "amount": <number>,
  "currency": "<string>",
  "category": "<string>",
  "payment_method": "<string or null>",
  "merchant": "<string or null>",
  "description": "<original text summarized>"
}}

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "COP".
- category: infer from context. Use one of: {cats}.
- payment_method: if mentioned (e.g., "tarjeta", "efectivo", "nequi"), include it. Otherwise null.
- merchant: if a specific place or brand is mentioned, include it. Otherwise null.
- description: a short summary of what the expense was.
- Return ONLY valid JSON matching the format above. No extra text, no markdown.

Examples:
Query: "almuerzo 32000"
Response:
{{"expenses": [{{"amount": 32000, "currency": "COP", "category": "comida", "payment_method": null, "merchant": null, "description": "almuerzo"}}]}}

Query: "uber 14 lukas"
Response:
{{"expenses": [{{"amount": 14000, "currency": "COP", "category": "transporte", "payment_method": null, "merchant": "Uber", "description": "viaje en Uber"}}]}}

Query: "mercado 12 mil con tarjeta"
Response:
{{"expenses": [{{"amount": 12000, "currency": "COP", "category": "mercado", "payment_method": "tarjeta", "merchant": null, "description": "compra en el mercado"}}]}}

Query: "almuerzo 20 luca y cine 40 mil"
Response:
{{"expenses": [{{"amount": 20000, "currency": "COP", "category": "comida", "payment_method": null, "merchant": null, "description": "almuerzo"}}, {{"amount": 40000, "currency": "COP", "category": "entretenimiento", "payment_method": null, "merchant": null, "description": "cine"}}]}}
"""


def parse_expense(text: str, categories: list[str] | None = None) -> list[dict]:
    """Send the user's message to the LLM and return a list of structured expense data."""
    logger.info("Calling OpenAI to parse: %s", text)
    response = client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        messages=[
            {"role": "system", "content": _build_system_prompt(categories)},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content.strip()
    logger.debug("OpenAI raw response: %s", content)
    parsed_list = json.loads(content)["expenses"]

    # Attach metadata to each expense
    today = datetime.now(_BOGOTA).date().isoformat()
    for item in parsed_list:
        item["date"] = today
        item["confidence"] = estimate_confidence(item)

    return parsed_list


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Send audio bytes to OpenAI Whisper and return the transcript string."""
    logger.info("Sending %d bytes to Whisper", len(audio_bytes))
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename  # OpenAI SDK uses the name to detect the format
    transcript = client.audio.transcriptions.create(
        model=OPENAI_AUDIO_MODEL,
        file=audio_file,
        language=OPENAI_AUDIO_LANGUAGE,
    )
    logger.info("Whisper transcript: %s", transcript.text)
    return transcript.text


def parse_expense_from_image(
    image_bytes: bytes,
    content_type: str,
    caption: str = "",
    categories: list[str] | None = None,
) -> list[dict]:
    """Extract expenses from an image using GPT-4o vision.

    Args:
        image_bytes: Raw image bytes.
        content_type: MIME type (e.g. 'image/jpeg').
        caption: Optional text the user sent alongside the image.
        categories: The user's expense categories to inject into the prompt.

    Returns:
        Same list[dict] format as parse_expense.
    """
    logger.info("Sending image (%s, %d bytes) to GPT-4o vision", content_type, len(image_bytes))
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{content_type};base64,{image_b64}"

    user_content: list[dict] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if caption:
        user_content.append(
            {"type": "text", "text": f"Additional context from the user: '{caption}'"}
        )

    response = client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        messages=[
            {"role": "system", "content": _build_system_prompt(categories)},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content.strip()
    logger.info("GPT-4o vision raw response: %s", content)
    parsed_list = json.loads(content)["expenses"]

    today = datetime.now(_BOGOTA).date().isoformat()
    for item in parsed_list:
        item["date"] = today
        item["confidence"] = estimate_confidence(item)

    return parsed_list




def parse_report_request(text: str, current_datetime: str) -> dict:
    """Use OpenAI to extract the min/max date range from a report request.

    Args:
        text: The user's original message.
        current_datetime: ISO datetime string injected for relative date resolution.

    Returns:
        {"min_date": "YYYY-MM-DD", "max_date": "YYYY-MM-DD"}
    """
    prompt = f"""Today is {current_datetime} (Colombia time, COT UTC-5).

The user is asking for a spending report. Extract the date range they are referring to.
Return ONLY a JSON object with exactly two fields:
{{"min_date": "YYYY-MM-DD", "max_date": "YYYY-MM-DD"}}

Rules:
- If no date range is mentioned, default to the first day of the current month through today.
- "esta semana" → Monday of the current week through today.
- "la semana pasada" → Monday through Sunday of last week.
- "este mes" or no qualifier → first day of current month through today.
- "el mes pasado" → full prior calendar month (first to last day).
- "últimos N días" / "los últimos N días" → today minus N days through today.
- A specific month name (e.g. "marzo", "abril") → full month range, capped at today if current month.
- Return ONLY valid JSON. No extra text, no markdown."""

    logger.info("Calling OpenAI to extract report date range from: %s", text)
    response = client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content.strip()
    logger.debug("OpenAI report date range response: %s", content)
    return json.loads(content)
