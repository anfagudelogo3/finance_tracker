import json
import logging
from datetime import date

from openai import OpenAI

from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """You are a personal finance assistant that extracts expense data from short text messages in Spanish or English.

Given a user message, extract the following fields and return ONLY a JSON object:

{
  "amount": <number>,
  "category": "<string>",
  "payment_method": "<string or null>",
  "merchant": "<string or null>",
  "description": "<original text summarized>"
}

Rules:
- amount: the numeric value. No currency symbols.
- category: infer from context. Use one of: comida, transporte, mercado, salud, entretenimiento, hogar, educacion, ropa, servicios, otro.
- payment_method: if mentioned (e.g., "tarjeta", "efectivo", "nequi"), include it. Otherwise null.
- merchant: if a specific place or brand is mentioned, include it. Otherwise null.
- description: a short summary of what the expense was.
- Return ONLY valid JSON. No extra text, no markdown."""


def parse_expense(text: str) -> dict:
    """Send the user's message to the LLM and return structured expense data."""
    logger.info("Calling OpenAI to parse: %s", text)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )

    content = response.choices[0].message.content.strip()
    logger.debug("OpenAI raw response: %s", content)
    parsed = json.loads(content)

    # Attach metadata
    parsed["date"] = date.today().isoformat()
    parsed["confidence"] = _estimate_confidence(parsed)

    return parsed


def _estimate_confidence(parsed: dict) -> float:
    """Simple heuristic confidence score based on field completeness."""
    score = 0.0
    if parsed.get("amount") and isinstance(parsed["amount"], (int, float)):
        score += 0.5
    if parsed.get("category") and parsed["category"] != "otro":
        score += 0.3
    if parsed.get("description"):
        score += 0.2
    return round(score, 2)
