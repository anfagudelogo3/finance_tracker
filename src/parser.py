import json
import logging
import unicodedata
from datetime import date
from difflib import get_close_matches

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


# Single-word triggers eligible for fuzzy matching
_REPORT_KEYWORDS_SINGLE = [
    # Spanish
    "reporte", "resumen", "informe", "analisis", "estadisticas", "estadistica",
    "balance", "historial", "desglose", "gastos",
    # English
    "report", "summary", "breakdown", "spending", "expenses", "analytics",
]

# Multi-word phrases: exact substring match only (fuzzy on phrases risks false positives)
_REPORT_KEYWORDS_PHRASE = [
    # Spanish — questions
    "cuanto gaste", "cuanto llevo", "cuanto he gastado", "cuanto va",
    "en que gaste", "en que he gastado", "como voy", "como van mis",
    # Spanish — possessives
    "mis gastos", "mis finanzas", "mi resumen", "mi reporte", "mi balance",
    # Spanish — scoped
    "gastos del", "gastos de", "gastos en", "gasto del", "gasto de",
    "resumen del", "resumen de", "informe del", "informe de",
    # Spanish — time expressions that imply a report
    "esta semana gaste", "este mes gaste",
    # English
    "how much did i spend", "how much have i spent", "what did i spend",
    "my expenses", "my spending", "expenses from", "spending this",
    "show me my", "give me a",
]

_FUZZY_CUTOFF = 0.8


def _normalize(text: str) -> str:
    """Lowercase and strip accents for accent-insensitive keyword matching."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def is_report_request(text: str) -> bool:
    """Return True if the message is asking for a spending report.

    Two-pass strategy:
    1. Exact substring match against all keywords (free, fast).
    2. If no match, fuzzy word-level match against single-word triggers using
       difflib.get_close_matches at cutoff=0.8 (stdlib, no extra dependencies).
    """
    normalized = _normalize(text)

    # Pass 1: exact match on single words and phrases
    all_keywords = _REPORT_KEYWORDS_SINGLE + _REPORT_KEYWORDS_PHRASE
    if any(kw in normalized for kw in all_keywords):
        logger.debug("Report request detected via exact match")
        return True

    # Pass 2: fuzzy match each token against single-word triggers only
    tokens = normalized.split()
    for token in tokens:
        if get_close_matches(token, _REPORT_KEYWORDS_SINGLE, n=1, cutoff=_FUZZY_CUTOFF):
            logger.debug("Report request detected via fuzzy match on token '%s'", token)
            return True

    return False


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
