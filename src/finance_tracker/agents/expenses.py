import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.core.dates import DATE_RULE, resolve_record_date, today_line
from finance_tracker.core.claude_client import client
from finance_tracker.config import (
    CLAUDE_EXTRACTION_MODEL,
    DEFAULT_CURRENCY,
    TIMEZONE,
    DEFAULT_EXPENSE_CATEGORIES,
    MSG_EMPTY_EXPENSE,
    MSG_FUTURE_EXPENSE,
)
from finance_tracker.storage.database import get_user_categories, save_expense
from finance_tracker.core.confidence import estimate_confidence
from finance_tracker.channels.whatsapp.sender import format_confirmation

logger = logging.getLogger(__name__)

_BOGOTA = ZoneInfo(TIMEZONE)


def _build_system_prompt(
    categories: list[str], currency: str = DEFAULT_CURRENCY, today: date | None = None
) -> str:
    """Field-extraction rules ported from legacy/openai_parser.py's prompt — same domain logic,
    just no JSON-shape instructions since the record_expenses tool enforces that."""
    cats = ", ".join(categories)
    today_text = today_line(today or datetime.now(_BOGOTA).date())
    return f"""You are a personal finance assistant that extracts expense data from short text messages in Spanish or English.

Given a user message, call the record_expenses tool with one entry per expense mentioned. If only one expense is mentioned, pass an array with one entry. If the message does not describe any expense, call record_expenses with an empty expenses array.

{today_text}

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "{currency}".
- category: infer from context. Use one of: {cats}.
- payment_method: if mentioned (e.g., "tarjeta", "efectivo", "nequi"), include it. Otherwise null.
- merchant: if a specific place or brand is mentioned, include it. Otherwise null.
{DATE_RULE}
- description: a short summary of what the expense was.

Examples:
Query: "almuerzo 32000"
→ one expense: amount=32000, currency={currency}, category=comida, payment_method=null, merchant=null, description="almuerzo"

Query: "uber 14 lukas"
→ one expense: amount=14000, currency={currency}, category=transporte, payment_method=null, merchant="Uber", description="viaje en Uber"

Query: "mercado 12 mil con tarjeta"
→ one expense: amount=12000, currency={currency}, category=mercado, payment_method="tarjeta", merchant=null, description="compra en el mercado"

Query: "almuerzo 20 luca y cine 40 mil"
→ two expenses: (amount=20000, currency={currency}, category=comida, description="almuerzo") and (amount=40000, currency={currency}, category=entretenimiento, description="cine")

Query: "gracias"
→ no expense: call record_expenses with an empty expenses array
"""


def _build_expense_tool(categories: list[str]) -> dict:
    """Tool schema enforcing the same field shape legacy/openai_parser.py's prompt-described JSON
    used, but schema-validated by the API instead of trusted to json.loads."""
    return {
        "name": "record_expenses",
        "description": "Record the expense(s) extracted from the user's message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expenses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {
                                "type": "number",
                                "description": "Numeric amount, no currency symbols.",
                            },
                            "currency": {
                                "type": "string",
                                "description": "e.g. COP, USD. Default COP if not mentioned.",
                            },
                            "category": {
                                "type": "string",
                                "enum": categories,
                            },
                            "payment_method": {
                                "type": ["string", "null"],
                                "description": "e.g. tarjeta, efectivo, nequi. null if not mentioned.",
                            },
                            "merchant": {
                                "type": ["string", "null"],
                                "description": "Specific place or brand, if mentioned. Otherwise null.",
                            },
                            "date_text": {
                                "type": ["string", "null"],
                                "description": "The exact words that say when it happened (ayer, el 28, el 3 de octubre); null if none.",
                            },
                            "date": {
                                "type": ["string", "null"],
                                "description": "YYYY-MM-DD when the message says when it happened; null otherwise.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Short summary of the expense.",
                            },
                        },
                        "required": [
                            "amount",
                            "currency",
                            "category",
                            "payment_method",
                            "merchant",
                            "date_text",
                            "date",
                            "description",
                        ],
                    },
                },
            },
            "required": ["expenses"],
        },
    }


def _extract(
    text: str,
    categories: list[str],
    currency: str = DEFAULT_CURRENCY,
    today: date | None = None,
) -> list[dict]:
    """Call Claude with a forced tool call so the response is schema-validated by the
    API rather than trusted to json.loads (today's legacy/openai_parser.py has no error handling
    around that parse — this closes that gap)."""
    cats = categories or DEFAULT_EXPENSE_CATEGORIES
    logger.info("Calling Claude to parse: %s", text)
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=1024,
        system=_build_system_prompt(cats, currency, today),
        messages=[{"role": "user", "content": text}],
        tools=[_build_expense_tool(cats)],
        tool_choice={"type": "tool", "name": "record_expenses"},
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    logger.debug("Claude tool_use input: %s", tool_use.input)
    return tool_use.input["expenses"]


def handle(request: AgentRequest) -> AgentResponse:
    """Identify, extract, classify, and persist expense(s) from a text or
    audio-transcript message. Image expenses stay on the old OpenAI vision path."""
    categories = get_user_categories(request.user_id, kind="expense")
    # The message's own Bogota date: "ayer" is relative to when it was written (a
    # message held during onboarding keeps its date). created_at is the ingest time.
    message_date = datetime.fromisoformat(request.now).astimezone(_BOGOTA).date()
    expenses = _extract(
        request.text, categories, request.default_currency, message_date
    )
    if not expenses:
        logger.info("No expenses parsed from message id=%d", request.message_id)
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_EXPENSE, data=None)

    saved, future = [], []
    for expense in expenses:
        record_date, is_future = resolve_record_date(
            expense.get("date"), message_date, expense.get("date_text")
        )
        if is_future:
            future.append({**expense, "date": record_date.isoformat()})
            continue
        expense["date"] = record_date.isoformat()
        expense["confidence"] = estimate_confidence(expense)
        expense["source"] = request.message_type
        expense["expense_id"] = save_expense(
            request.message_id, expense, request.user_id
        )
        logger.info(
            "Expense saved: id=%d amount=%s category=%s confidence=%s source=%s",
            expense["expense_id"],
            expense.get("amount"),
            expense.get("category"),
            expense.get("confidence"),
            request.message_type,
        )
        saved.append(expense)

    return AgentResponse(
        ok=True, reply_text=_reply(saved, future), data=saved or None
    )


def _reply(saved: list[dict], future: list[dict]) -> str:
    """Confirmation for what was saved, plus a note for each future-dated expense
    (not recorded: it hasn't happened yet)."""
    parts = [format_confirmation(saved)] if saved else []
    parts += [
        MSG_FUTURE_EXPENSE.format(
            currency=e.get("currency", "COP"),
            amount=f"{e['amount']:,.0f}".replace(",", "."),
            description=e.get("description") or e.get("category", ""),
            date=e["date"],
        )
        for e in future
    ]
    return "\n\n".join(parts)
