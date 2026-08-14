import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from agent_types import AgentRequest, AgentResponse
from claude_client import client
from config import CLAUDE_EXTRACTION_MODEL, DEFAULT_INCOME_CATEGORIES, MSG_EMPTY_INCOME
from database import get_user_categories, save_income
from parser import _estimate_confidence
from whatsapp import format_income_confirmation

logger = logging.getLogger(__name__)

_BOGOTA = ZoneInfo("America/Bogota")


def _build_system_prompt(categories: list[str]) -> str:
    """Field-extraction rules for income — no payment_method/merchant, since the
    incomes table has no such columns (unlike expenses)."""
    cats = ", ".join(categories)
    return f"""You are a personal finance assistant that extracts income data (money received) from short text messages in Spanish or English.

Given a user message, call the record_incomes tool with one entry per income mentioned. If only one income is mentioned, pass an array with one entry. If the message does not describe any income received, call record_incomes with an empty incomes array.

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "COP".
- category: infer from context. Use one of: {cats}.
- description: a short summary of what the income was.

Examples:
Query: "me pagaron 2000000 de salario"
→ one income: amount=2000000, currency=COP, category=salario, description="pago de salario"

Query: "cobre 500000 de un cliente"
→ one income: amount=500000, currency=COP, category=freelance, description="pago de cliente"

Query: "me pagaron 500 dolares de un cliente"
→ one income: amount=500, currency=USD, category=freelance, description="pago de cliente"

Query: "me depositaron mi salario de 3000000 y tambien 200000 de un regalo"
→ two incomes: (amount=3000000, currency=COP, category=salario, description="salario") and (amount=200000, currency=COP, category=regalo, description="regalo")

Query: "gracias"
→ no income: call record_incomes with an empty incomes array
"""


def _build_income_tool(categories: list[str]) -> dict:
    """Tool schema enforcing the exact field shape incomes persists — no
    payment_method/merchant (expense-only columns), schema-validated by the API."""
    return {
        "name": "record_incomes",
        "description": "Record the income(s) extracted from the user's message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incomes": {
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
                            "description": {
                                "type": "string",
                                "description": "Short summary of the income.",
                            },
                        },
                        "required": ["amount", "currency", "category", "description"],
                    },
                },
            },
            "required": ["incomes"],
        },
    }


def _extract(text: str, categories: list[str]) -> list[dict]:
    """Forced tool-use call, same pattern as expense_agent._extract."""
    cats = categories or DEFAULT_INCOME_CATEGORIES
    logger.info("Calling Claude to parse income: %s", text)
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=1024,
        system=_build_system_prompt(cats),
        messages=[{"role": "user", "content": text}],
        tools=[_build_income_tool(cats)],
        tool_choice={"type": "tool", "name": "record_incomes"},
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    logger.debug("Claude tool_use input: %s", tool_use.input)
    return tool_use.input["incomes"]


def handle(request: AgentRequest) -> AgentResponse:
    """Identify, extract, classify, and persist income(s) from a text or
    audio-transcript message. Image/receipt income (e.g. deposit slips) isn't
    classified yet — all image messages still route to the expense image path."""
    categories = get_user_categories(request.user_id, kind="income")
    incomes = _extract(request.text, categories)
    if not incomes:
        logger.info("No incomes parsed from message id=%d", request.message_id)
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_INCOME, data=None)

    today = datetime.now(_BOGOTA).date().isoformat()
    for income in incomes:
        income["date"] = today
        income["confidence"] = _estimate_confidence(income)
        # incomes.source means the money's origin ('manual'|'gmail'|'bank'), not the
        # WhatsApp channel — every Phase 2 income arrives as a typed/spoken message.
        income["source"] = "manual"
        income["income_id"] = save_income(request.message_id, income, request.user_id)
        logger.info(
            "Income saved: id=%d amount=%s category=%s confidence=%s",
            income["income_id"],
            income.get("amount"),
            income.get("category"),
            income.get("confidence"),
        )

    return AgentResponse(
        ok=True, reply_text=format_income_confirmation(incomes), data=incomes
    )
