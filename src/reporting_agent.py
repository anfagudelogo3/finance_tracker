import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent_types import AgentRequest, AgentResponse
from claude_client import client
from config import CLAUDE_EXTRACTION_MODEL
from database import get_expenses
from parser import _normalize
from reporting import format_report

logger = logging.getLogger(__name__)

_BOGOTA = ZoneInfo("America/Bogota")

_MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_LAST_WEEK_PHRASES = ["semana pasada"]
_THIS_WEEK_PHRASES = ["esta semana", "this week"]
_LAST_MONTH_PHRASES = ["mes pasado", "last month"]
_THIS_MONTH_PHRASES = ["este mes", "this month"]
_TODAY_PHRASES = ["hoy", "today"]
_AMBIGUOUS_SIGNAL_WORDS = ["entre", "desde", "hasta", "between", "from", "since"]

# Referential follow-ups ("¿y la anterior?") have no date content of their own — without
# this check they fall through every rule above and hit the final "no signal → apply the
# default" branch, silently returning the wrong range instead of asking Claude (which can
# resolve them against conversation history). Same "don't guess" principle as
# _AMBIGUOUS_SIGNAL_WORDS, just a different reason to bail.
_REFERENTIAL_SIGNAL_WORDS = [
    "anterior",
    "esa misma",
    "ese mismo",
    "la misma",
    "eso",
    "previous",
    "that",
    "same",
]

_LAST_N_DAYS_RE = re.compile(r"(?:ultimos?|last)\s+(\d+)\s+(?:dias|days)")


def _resolve_deterministic(text: str, now: datetime) -> dict | None:
    """Exact timedelta/weekday arithmetic for the enumerable set of relative-date
    phrases already covered by the eval dataset. Returns None if nothing matches,
    signaling the caller to fall back to Claude — never guesses.

    This is the fix for parse_report_request's two observed failures: a missing
    "hoy" rule (silently fell back to the month default) and unreliable LLM weekday
    arithmetic for "la semana pasada" — both are exact, zero-ambiguity Python
    computations once you stop asking an LLM to do calendar math in free text.
    """
    normalized = _normalize(text)
    today = now.date()

    if any(p in normalized for p in _LAST_WEEK_PHRASES):
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday = this_monday - timedelta(days=1)
        return {
            "min_date": last_monday.isoformat(),
            "max_date": last_sunday.isoformat(),
        }

    if any(p in normalized for p in _THIS_WEEK_PHRASES):
        this_monday = today - timedelta(days=today.weekday())
        return {"min_date": this_monday.isoformat(), "max_date": today.isoformat()}

    if any(p in normalized for p in _LAST_MONTH_PHRASES):
        first_this_month = today.replace(day=1)
        last_day_prev_month = first_this_month - timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        return {
            "min_date": first_day_prev_month.isoformat(),
            "max_date": last_day_prev_month.isoformat(),
        }

    n_days_match = _LAST_N_DAYS_RE.search(normalized)
    if n_days_match:
        n = int(n_days_match.group(1))
        return {
            "min_date": (today - timedelta(days=n)).isoformat(),
            "max_date": today.isoformat(),
        }

    if any(p in normalized for p in _TODAY_PHRASES):
        return {"min_date": today.isoformat(), "max_date": today.isoformat()}

    # Explicit range words ("entre"/"desde"/"hasta") signal the user wants a precise
    # range, not a whole-month heuristic — check this before the month-name loop so a
    # query like "entre el 3 de marzo y el 20 de abril" doesn't get short-circuited by
    # "marzo" matching a bare whole-month rule. Bail to the Claude fallback instead.
    if any(w in normalized for w in _AMBIGUOUS_SIGNAL_WORDS):
        return None

    for month_name, month_num in _MONTHS_ES.items():
        if month_name not in normalized:
            continue
        year = today.year if month_num <= today.month else today.year - 1
        first_day = today.replace(year=year, month=month_num, day=1)
        if month_num == 12:
            next_month_first = first_day.replace(year=year + 1, month=1, day=1)
        else:
            next_month_first = first_day.replace(month=month_num + 1, day=1)
        last_day = next_month_first - timedelta(days=1)
        if (year, month_num) == (today.year, today.month):
            last_day = min(last_day, today)
        return {"min_date": first_day.isoformat(), "max_date": last_day.isoformat()}

    if any(p in normalized for p in _THIS_MONTH_PHRASES):
        return {
            "min_date": today.replace(day=1).isoformat(),
            "max_date": today.isoformat(),
        }

    # Nothing matched, and no range word was present (checked above). A leftover digit
    # means a date-shaped expression we still don't recognize (e.g. a specific day
    # without a range word); a referential word means this depends on conversation
    # history ("¿y la anterior?"). Either way, don't guess — fall back to Claude.
    # Otherwise this is genuinely unqualified ("resumen") — the existing default applies.
    if any(ch.isdigit() for ch in normalized) or any(
        w in normalized for w in _REFERENTIAL_SIGNAL_WORDS
    ):
        return None

    return {"min_date": today.replace(day=1).isoformat(), "max_date": today.isoformat()}


def _build_fallback_tool() -> dict:
    return {
        "name": "record_date_range",
        "description": "Resolve the date range for a spending report request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD, inclusive start date.",
                },
                "max_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD, inclusive end date.",
                },
            },
            "required": ["min_date", "max_date"],
        },
    }


def _resolve_with_claude(text: str, now: datetime, conversation: list[dict]) -> dict:
    """Fallback for date expressions the deterministic rules don't cover: specific
    date mentions, explicit ranges, and referential follow-ups ("¿y la anterior?") that
    need conversation history to resolve. Forced tool-use, not prompt-only JSON, same
    pattern as expense_agent/income_agent."""
    now_str = now.strftime("%Y-%m-%d (%A)")
    system = f"""Today is {now_str} (America/Bogota). The user is asking for a spending report. Resolve the exact date range they're referring to and call record_date_range.

Rules:
- If only a start date is given (e.g. "desde el 1 de mayo"), the end date is today.
- If only an end date is given, the start date is the first day of that month.
- Dates without a year are assumed to be the most recent occurrence not in the future.
- Both dates are inclusive, format YYYY-MM-DD.
- If the message refers to a previous report without giving its own date expression
  ("la anterior", "esa misma", "eso"), use the conversation history to figure out what
  period is being referred to — including dates already stated in a previous report's
  own text — and resolve relative to that.
"""
    logger.info("Calling Claude to resolve ambiguous date range: %s", text)
    messages = list(conversation) + [{"role": "user", "content": text}]
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=256,
        system=system,
        messages=messages,
        tools=[_build_fallback_tool()],
        tool_choice={"type": "tool", "name": "record_date_range"},
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    logger.debug("Claude tool_use input: %s", tool_use.input)
    return tool_use.input


def resolve_date_range(
    text: str, now: datetime, conversation: list[dict] | None = None
) -> dict:
    """Resolve the date range for a report request. Deterministic rules cover the
    entire known eval dataset — only text matching none of them pays for a Claude call.
    `conversation` (prior turns, oldest first) is only used by that fallback call, for
    referential follow-ups — self-sufficient phrases never need it, even follow-up-shaped
    ones like "la semana pasada"."""
    deterministic = _resolve_deterministic(text, now)
    if deterministic is not None:
        return deterministic
    return _resolve_with_claude(text, now, conversation or [])


def handle(request: AgentRequest) -> AgentResponse:
    """Read and aggregate existing expenses — no persistence. AgentResponse.data holds
    the query results used to build the report, not a newly-created row (unlike
    expense_agent/income_agent's extract-then-persist shape)."""
    now = datetime.fromisoformat(request.now)
    date_range = resolve_date_range(request.text, now, request.conversation)
    expenses = get_expenses(
        request.user_id, date_range["min_date"], date_range["max_date"]
    )
    logger.info(
        "Report for user=%d: %d expense(s) between %s and %s",
        request.user_id,
        len(expenses),
        date_range["min_date"],
        date_range["max_date"],
    )
    report = format_report(expenses, date_range["min_date"], date_range["max_date"])
    return AgentResponse(ok=True, reply_text=report, data=expenses)
