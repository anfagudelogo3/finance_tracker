import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from agent_types import AgentRequest, AgentResponse
from claude_client import client
from config import (
    CLAUDE_EXTRACTION_MODEL,
    DEFAULT_EXPENSE_CATEGORIES,
    MSG_EMPTY_EXPENSE,
)
from database import save_expense
from parser import _estimate_confidence
from whatsapp import format_confirmation

logger = logging.getLogger(__name__)

_BOGOTA = ZoneInfo("America/Bogota")


def _build_system_prompt(categories: list[str]) -> str:
    """Field-extraction rules ported from parser.py's prompt — same domain logic,
    just no JSON-shape instructions since the record_expenses tool enforces that."""
    cats = ", ".join(categories)
    return f"""You are a personal finance assistant that extracts expense data from short text messages in Spanish or English.

Given a user message, call the record_expenses tool with one entry per expense mentioned. If only one expense is mentioned, pass an array with one entry. If the message does not describe any expense, call record_expenses with an empty expenses array.

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "COP".
- category: infer from context. Use one of: {cats}.
- payment_method: if mentioned (e.g., "tarjeta", "efectivo", "nequi"), include it. Otherwise null.
- merchant: if a specific place or brand is mentioned, include it. Otherwise null.
- description: a short summary of what the expense was.

Examples:
Query: "almuerzo 32000"
→ one expense: amount=32000, currency=COP, category=comida, payment_method=null, merchant=null, description="almuerzo"

Query: "uber 14 lukas"
→ one expense: amount=14000, currency=COP, category=transporte, payment_method=null, merchant="Uber", description="viaje en Uber"

Query: "mercado 12 mil con tarjeta"
→ one expense: amount=12000, currency=COP, category=mercado, payment_method="tarjeta", merchant=null, description="compra en el mercado"

Query: "almuerzo 20 luca y cine 40 mil"
→ two expenses: (amount=20000, currency=COP, category=comida, description="almuerzo") and (amount=40000, currency=COP, category=entretenimiento, description="cine")

Query: "gracias"
→ no expense: call record_expenses with an empty expenses array
"""


def _build_expense_tool(categories: list[str]) -> dict:
    """Tool schema enforcing the same field shape parser.py's prompt-described JSON
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
                            "description",
                        ],
                    },
                },
            },
            "required": ["expenses"],
        },
    }


def _extract(text: str, categories: list[str]) -> list[dict]:
    """Call Claude with a forced tool call so the response is schema-validated by the
    API rather than trusted to json.loads (today's parser.py has no error handling
    around that parse — this closes that gap)."""
    cats = categories or DEFAULT_EXPENSE_CATEGORIES
    logger.info("Calling Claude to parse: %s", text)
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=1024,
        system=_build_system_prompt(cats),
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
    expenses = _extract(request.text, request.categories)
    if not expenses:
        logger.info("No expenses parsed from message id=%d", request.message_id)
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_EXPENSE, data=None)

    today = datetime.now(_BOGOTA).date().isoformat()
    for expense in expenses:
        expense["date"] = today
        expense["confidence"] = _estimate_confidence(expense)
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

    return AgentResponse(
        ok=True, reply_text=format_confirmation(expenses), data=expenses
    )
