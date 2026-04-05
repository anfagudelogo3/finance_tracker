import json
import logging
import unicodedata
from datetime import date

from openai import OpenAI

from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """You are a personal finance assistant that extracts expense data from short text messages in Spanish or English.

Given a user message, extract ALL expenses mentioned and return ONLY a JSON array. Each object in the array represents one expense. If only one expense is mentioned, return an array with one object.

Each expense object has these fields:
{
  "amount": <number>,
  "currency": "<string>",
  "category": "<string>",
  "payment_method": "<string or null>",
  "merchant": "<string or null>",
  "description": "<original text summarized>"
}

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "COP".
- category: infer from context. Use one of: comida, transporte, mercado, salud, entretenimiento, hogar, educacion, ropa, servicios, otro.
- payment_method: if mentioned (e.g., "tarjeta", "efectivo", "nequi"), include it. Otherwise null.
- merchant: if a specific place or brand is mentioned, include it. Otherwise null.
- description: a short summary of what the expense was.
- Return ONLY valid JSON. No extra text, no markdown.

Examples:
Query: "almuerzo 32000"
Response:
[{"amount": 32000, "currency": "COP", "category": "comida", "payment_method": null, "merchant": null, "description": "almuerzo"}]

Query: "uber 14 lukas"
Response:
[{"amount": 14000, "currency": "COP", "category": "transporte", "payment_method": null, "merchant": "Uber", "description": "viaje en Uber"}]

Query: "mercado 12 mil con tarjeta"
Response:
[{"amount": 12000, "currency": "COP", "category": "mercado", "payment_method": "tarjeta", "merchant": null, "description": "compra en el mercado"}]

Query: "almuerzo 20 luca y cine 40 mil"
Response:
[{"amount": 20000, "currency": "COP", "category": "comida", "payment_method": null, "merchant": null, "description": "almuerzo"}, {"amount": 40000, "currency": "COP", "category": "entretenimiento", "payment_method": null, "merchant": null, "description": "cine"}]
"""


def parse_expense(text: str) -> list[dict]:
    """Send the user's message to the LLM and return a list of structured expense data."""
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
    parsed_list = json.loads(content)

    # Attach metadata to each expense
    today = date.today().isoformat()
    for item in parsed_list:
        item["date"] = today
        item["confidence"] = _estimate_confidence(item)

    return parsed_list


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


_REPORT_KEYWORDS = [
    "reporte", "resumen", "informe",
    "cuanto gaste", "cuanto llevo", "analisis",
    "mis gastos", "gastos del", "gastos de",
    "report","summary", "how much did I spend",
    "my expenses","expenses from","analisis of my spending"
]


def _normalize(text: str) -> str:
    """Lowercase and strip accents for accent-insensitive keyword matching."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def is_report_request(text: str) -> bool:
    """Return True if the message is asking for a spending report."""
    normalized = _normalize(text)
    return any(kw in normalized for kw in _REPORT_KEYWORDS)


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
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content.strip()
    logger.debug("OpenAI report date range response: %s", content)
    return json.loads(content)
