import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.core.dates import DATE_RULE, resolve_record_date, today_line
from finance_tracker.core.claude_client import client
from finance_tracker.config import (
    CLAUDE_EXTRACTION_MODEL,
    DEFAULT_CURRENCY,
    DEFAULT_INCOME_CATEGORIES,
    MSG_EMPTY_INCOME,
    MSG_FUTURE_INCOME,
    TIMEZONE,
)
from finance_tracker.storage.database import get_user_categories, save_income
from finance_tracker.core.confidence import estimate_confidence
from finance_tracker.channels.whatsapp.sender import format_income_confirmation

logger = logging.getLogger(__name__)

_BOGOTA = ZoneInfo(TIMEZONE)


def _build_system_prompt(
    categories: list[str], currency: str = DEFAULT_CURRENCY, today: date | None = None
) -> str:
    """Field-extraction rules for income — no payment_method/merchant, since the
    incomes table has no such columns (unlike expenses)."""
    cats = ", ".join(categories)
    today_text = today_line(today or datetime.now(_BOGOTA).date())
    return f"""You are a personal finance assistant that extracts income data (money received) from short text messages in Spanish or English.

Given a user message, call the record_incomes tool with one entry per income mentioned. If only one income is mentioned, pass an array with one entry. If the message does not describe any income received, call record_incomes with an empty incomes array.

{today_text}

Rules:
- amount: the numeric value. No currency symbols.
- currency: the currency of the amount (e.g., "COP", "USD"). If the currency is not explicitly mentioned, assume "{currency}".
- category: infer from context. Use one of: {cats}.
{DATE_RULE}
- description: a short summary of what the income was.

Examples:
Query: "me pagaron 2000000 de salario"
→ one income: amount=2000000, currency={currency}, category=salario, description="pago de salario"

Query: "cobre 500000 de un cliente"
→ one income: amount=500000, currency={currency}, category=freelance, description="pago de cliente"

Query: "me pagaron 500 dolares de un cliente"
→ one income: amount=500, currency=USD, category=freelance, description="pago de cliente"

Query: "me depositaron mi salario de 3000000 y tambien 200000 de un regalo"
→ two incomes: (amount=3000000, currency={currency}, category=salario, description="salario") and (amount=200000, currency={currency}, category=regalo, description="regalo")

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
                                "description": "Short summary of the income.",
                            },
                        },
                        "required": ["amount", "currency", "category", "date_text", "date", "description"],
                    },
                },
            },
            "required": ["incomes"],
        },
    }


def _extract(
    text: str,
    categories: list[str],
    currency: str = DEFAULT_CURRENCY,
    today: date | None = None,
) -> list[dict]:
    """Forced tool-use call, same pattern as agents.expenses._extract."""
    cats = categories or DEFAULT_INCOME_CATEGORIES
    logger.info("Calling Claude to parse income: %s", text)
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=1024,
        system=_build_system_prompt(cats, currency, today),
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
    # The message's own Bogota date: "ayer" is relative to when it was written (a
    # message held during onboarding keeps its date). created_at is the ingest time.
    message_date = datetime.fromisoformat(request.now).astimezone(_BOGOTA).date()
    incomes = _extract(
        request.text, categories, request.default_currency, message_date
    )
    if not incomes:
        logger.info("No incomes parsed from message id=%d", request.message_id)
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_INCOME, data=None)

    saved, future = [], []
    for income in incomes:
        record_date, is_future = resolve_record_date(
            income.get("date"), message_date, income.get("date_text")
        )
        if is_future:
            future.append({**income, "date": record_date.isoformat()})
            continue
        income["date"] = record_date.isoformat()
        income["confidence"] = estimate_confidence(income)
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
        saved.append(income)

    return AgentResponse(ok=True, reply_text=_reply(saved, future), data=saved or None)


def _reply(saved: list[dict], future: list[dict]) -> str:
    """Confirmation for what was saved, plus a note for each future-dated income
    (not recorded: it hasn't happened yet)."""
    parts = [format_income_confirmation(saved)] if saved else []
    parts += [
        MSG_FUTURE_INCOME.format(
            currency=i.get("currency", "COP"),
            amount=f"{i['amount']:,.0f}".replace(",", "."),
            description=i.get("description") or i.get("category", ""),
            date=i["date"],
        )
        for i in future
    ]
    return "\n\n".join(parts)
