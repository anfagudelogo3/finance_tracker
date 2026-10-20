import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.core.dates import DATE_RULE, resolve_record_date, today_line
from finance_tracker.agents import records
from finance_tracker.channels.whatsapp.reply_context import as_prompt_block
from finance_tracker.core import agent_loop
from finance_tracker.core.claude_client import client
from finance_tracker.config import (
    CLAUDE_EXTRACTION_MODEL,
    MSG_ERROR,
    RECORD_AGENT_MAX_ITERATIONS,
    RECORD_AGENT_TIMEOUT_SECONDS,
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


def _run(
    text: str,
    categories: list[str],
    currency: str,
    today: date | None,
    execute_tool,
    replied_to: dict | None = None,
):
    """One agent turn on the tool loop; it ends when Claude ends its turn. A new record
    is record_expenses plus a closing call; corrections (find → update/delete) take one
    more. Replies are written in code from what the tools did."""
    cats = categories or DEFAULT_EXPENSE_CATEGORIES
    content = text
    if replied_to:
        content = f"{as_prompt_block(replied_to)}\n<message>{text}</message>"
    logger.info("Calling Claude (expense agent): %s", text)
    return agent_loop.run_tool_loop(
        client=client,
        model=CLAUDE_EXTRACTION_MODEL,
        system=_build_system_prompt(cats, currency, today) + records.rules("expense"),
        tools=[_build_expense_tool(cats), *records.tools("expense")],
        messages=[{"role": "user", "content": content}],
        execute_tool=execute_tool,
        max_iterations=RECORD_AGENT_MAX_ITERATIONS,
        timeout_seconds=RECORD_AGENT_TIMEOUT_SECONDS,
        max_tokens=1024,
    )


def _extract(
    text: str,
    categories: list[str],
    currency: str = DEFAULT_CURRENCY,
    today: date | None = None,
) -> list[dict]:
    """What record_expenses would record for `text`, with nothing written — the evals'
    entry point, running the same prompt and tools as handle()."""
    captured: list[dict] = []

    def execute(name: str, tool_input: dict) -> str:
        if name == "record_expenses":
            captured.extend(tool_input.get("expenses") or [])
            return "Recorded."
        if name == "find_records":
            return "[]"
        if name == "ask_user":
            return "The question will be sent to the user."
        raise ValueError("corrections aren't available here")

    _run(text, categories, currency, today, execute)
    return captured


def handle(request: AgentRequest) -> AgentResponse:
    """Record new expenses from a text or audio-transcript message, or correct existing
    ones (Phase 8b: find, update, delete — agents/records.py)."""
    categories = get_user_categories(request.user_id, kind="expense")
    # The message's own Bogota date: "ayer" is relative to when it was written (a
    # message held during onboarding keeps its date). created_at is the ingest time.
    message_date = datetime.fromisoformat(request.now).astimezone(_BOGOTA).date()
    corrections = records.Corrections("expense", request)
    saved: list[dict] = []
    future: list[dict] = []

    def execute(name: str, tool_input: dict) -> str:
        if name == "record_expenses":
            corrections.check_new_record()
            new_saved, new_future = _save(
                tool_input.get("expenses") or [], request, message_date
            )
            saved.extend(new_saved)
            future.extend(new_future)
            return json.dumps({"recorded": len(new_saved), "future_not_recorded": len(new_future)})
        return corrections.execute(name, tool_input)

    outcome = _run(
        request.text, categories, request.default_currency, message_date, execute,
        request.replied_to,
    )
    parts = [_reply(saved, future)] if saved or future else []
    parts += corrections.replies
    if parts:
        return AgentResponse(ok=True, reply_text="\n\n".join(parts), data=saved or None)
    if outcome.status == agent_loop.END_TURN:
        logger.info("Nothing to record or correct in message id=%d", request.message_id)
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_EXPENSE, data=None)
    return AgentResponse(ok=False, reply_text=MSG_ERROR, error=outcome.status)


def _save(items: list[dict], request: AgentRequest, message_date: date):
    """Persist the new expenses (dated from the message's words); future-dated ones
    aren't recorded. Returns (saved, future)."""
    saved, future = [], []
    for expense in items:
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
    return saved, future


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
