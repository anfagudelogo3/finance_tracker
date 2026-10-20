"""Deterministic keyword checks used for free routing (no LLM call).

Excel and report checks run in handler.py before the orchestrator; the income check
runs inside orchestrator.handle_message. Moved here from the old parser.py and orchestrator.py
so every keyword signal lives in one module.
"""

import logging
import re
from difflib import get_close_matches

from finance_tracker.config import FUZZY_MATCH_CUTOFF
from finance_tracker.core.text import normalize

logger = logging.getLogger(__name__)

_EXCEL_KEYWORDS = [
    # Explicit format names
    "excel",
    "xlsx",
    "spreadsheet",
    # Spanish — spreadsheet variants
    "hoja de calculo",
    "hoja de cálculo",
    "hoja calculo",
    "hoja calculo",
    "hojas de calculo",
    "hojas de cálculo",
    # Spanish export/download verbs
    "exportar",
    "exportame",
    "exporta",
    "descargar",
    "descarga",
    "descargame",
]


def is_excel_request(text: str) -> bool:
    """Return True if the user is asking for an Excel export."""
    normalized = normalize(text)
    return any(kw in normalized for kw in _EXCEL_KEYWORDS)


_REPORT_KEYWORDS_SINGLE = [
    # Spanish
    "reporte",
    "resumen",
    "informe",
    "analisis",
    "estadisticas",
    "estadistica",
    "balance",
    "historial",
    "desglose",
    "gastos",
    # English
    "report",
    "summary",
    "breakdown",
    "spending",
    "expenses",
    "analytics",
]

# Multi-word phrases: exact substring match only (fuzzy on phrases risks false positives)
_REPORT_KEYWORDS_PHRASE = [
    # Spanish — questions
    "cuanto gaste",
    "cuanto llevo",
    "cuanto he gastado",
    "cuanto va",
    "en que gaste",
    "en que he gastado",
    "como voy",
    "como van mis",
    # Spanish — possessives
    "mis gastos",
    "mis finanzas",
    "mi resumen",
    "mi reporte",
    "mi balance",
    # Spanish — scoped
    "gastos del",
    "gastos de",
    "gastos en",
    "gasto del",
    "gasto de",
    "resumen del",
    "resumen de",
    "informe del",
    "informe de",
    # Spanish — time expressions that imply a report
    "esta semana gaste",
    "este mes gaste",
    # English
    "how much did i spend",
    "how much have i spent",
    "what did i spend",
    "my expenses",
    "my spending",
    "expenses from",
    "spending this",
    "show me my",
    "give me a",
]


def is_report_request(text: str) -> bool:
    """Return True if the message is asking for a spending report.

    Two-pass strategy:
    1. Exact substring match against all keywords (free, fast).
    2. If no match, fuzzy word-level match against single-word triggers using
       difflib.get_close_matches at cutoff=0.8 (stdlib, no extra dependencies).
    """
    normalized = normalize(text)

    # Pass 1: exact match on single words and phrases
    all_keywords = _REPORT_KEYWORDS_SINGLE + _REPORT_KEYWORDS_PHRASE
    if any(kw in normalized for kw in all_keywords):
        logger.debug("Report request detected via exact match")
        return True

    # Pass 2: fuzzy match each token against single-word triggers only
    tokens = normalized.split()
    for token in tokens:
        if get_close_matches(
            token, _REPORT_KEYWORDS_SINGLE, n=1, cutoff=FUZZY_MATCH_CUTOFF
        ):
            logger.debug("Report request detected via fuzzy match on token '%s'", token)
            return True

    return False


# Phrase-level only, deliberately no fuzzy single-word matching like is_report_request
# uses: income and expense vocabulary collide on shared stems ("pagué" = I paid, an
# expense, vs. "me pagaron" = I was paid, income), so fuzzy matching risks false
# positives. Bare generic phrases ("mi pago", "pago de") are deliberately excluded too —
# "mi pago del arriendo" is rent, an expense — in favor of specific compounds.
_INCOME_KEYWORDS_PHRASE = [
    "me pagaron",
    "me pagó",
    "me depositaron",
    "me depositó",
    "me transfirieron",
    "me transfirió",
    "me consignaron",
    "recibi mi",
    "recibí mi",
    "recibi el",
    "recibí el",
    "cobre mi",
    "cobré mi",
    "mi salario",
    "mi sueldo",
    "mi quincena",
    "mi nomina",
    "mi nómina",
    "pago de salario",
    "pago de sueldo",
    "pago de nomina",
    "pago de nómina",
    "i got paid",
    "got paid",
    "my salary",
    "my paycheck",
]


def is_income_request(text: str) -> bool:
    """Return True if the message describes money received, not spent.

    Heuristic, not exhaustive — an income message that doesn't match any phrase falls
    through to agents.expenses (same as before this agent existed, not a regression). A
    real classification call is deferred until ambiguous cases actually show up.
    """
    normalized = normalize(text)
    return any(kw in normalized for kw in _INCOME_KEYWORDS_PHRASE)


# ── Phase 5: the narrowed fast paths in front of the router ────────────────────────
#
# A fast path has no LLM behind it to catch its mistakes, so it fires only when the
# intent is unambiguous and otherwise returns None and lets the router decide. The
# Phase 4 keyword checks above stay as-is for the signals they provide; fast_route adds
# the guards (docs/agent-roadmap.md §4.6). tests/routing/test_fast_paths.py checks
# that fast_route is never wrong on evals/datasets/route_message.json.

# Words that make a message multi-intent or a follow-up ("y otro de 15000", "almuerzo
# 20k y me pagaron 2M"). ", " too, but not "," alone — "1,5" is a number.
_CONJUNCTIONS = {"y", "e", "and", "tambien", "ademas", "luego"}

# Anything destructive, prompt-injection-like, or about someone else's data goes to the
# router (which can decline) — never straight to an agent.
_BLOCKED_WORDS = {
    "borra",
    "borrar",
    "borrame",
    "elimina",
    "eliminar",
    "eliminame",
    "delete",
    "ignora",
    "ignore",
    "instrucciones",
    "instructions",
    "prompt",
    "sistema",
    "system",
}
# "+57…" or a 10+ digit run (a phone number); amounts like 2000000 are shorter.
_PHONE_LIKE = re.compile(r"\+\d|\d{10,}")

# Clear spending words for the simple-expense fast path. Token match, not substring
# ("gas" must not match "gasolina", "compra" is not "compré").
_EXPENSE_WORDS = {
    # verbs
    "pague",
    "gaste",
    "compre",
    "paid",
    "spent",
    "bought",
    # food
    "almuerzo",
    "desayuno",
    "cena",
    "comida",
    "onces",
    "tinto",
    "cafe",
    "coffee",
    "lunch",
    "dinner",
    "breakfast",
    "mercado",
    "supermercado",
    "domicilio",
    "rappi",
    # transport
    "uber",
    "taxi",
    "bus",
    "transmilenio",
    "metro",
    "gasolina",
    "parqueadero",
    "peaje",
    "didi",
    "cabify",
    # home & services
    "arriendo",
    "rent",
    "luz",
    "agua",
    "internet",
    "celular",
    "netflix",
    "spotify",
    # health, leisure, other
    "farmacia",
    "drogueria",
    "medicina",
    "medico",
    "cine",
    "ropa",
    "zapatos",
    "gimnasio",
    "gym",
}
_MAX_SIMPLE_EXPENSE_WORDS = 7
_MAX_SINGLE_REPORT_WORD_MESSAGE = 3  # "resumen", "mi balance" — not "borra mis gastos"


def _tokens(normalized: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalized)


def _has_pending_question(conversation: list[dict] | None) -> bool:
    """True if the assistant's last turn asked something — the reply is an answer
    ("fue un gasto, mercado") that needs history, so the router handles it."""
    for turn in reversed(conversation or []):
        if turn.get("role") == "assistant":
            return turn.get("content", "").strip().endswith("?")
    return False


def _is_guarded(text: str, normalized: str, tokens: list[str]) -> bool:
    return (
        any(t in _CONJUNCTIONS for t in tokens)
        or ", " in text
        or ";" in text
        or any(t in _BLOCKED_WORDS for t in tokens)
        or bool(_PHONE_LIKE.search(normalized))
    )


def _is_fast_report(normalized: str, tokens: list[str]) -> bool:
    """Report phrases ("cuanto gaste", "mis gastos de") anywhere, single report words
    only in a tiny message. No fuzzy matching: it was the main source of misroutes."""
    if any(kw in normalized for kw in _REPORT_KEYWORDS_PHRASE):
        return True
    return len(tokens) <= _MAX_SINGLE_REPORT_WORD_MESSAGE and any(
        t in _REPORT_KEYWORDS_SINGLE for t in tokens
    )


def fast_route(
    text: str,
    profile: dict | None,
    conversation: list[dict] | None = None,
    replied_to: dict | None = None,
) -> str | None:
    """Return "reporting" | "incomes" | "expenses" when the message can skip the
    router, or None to send it to the router.

    Only for personal profiles: the keywords know nothing about a business ("almuerzo
    20 mil" can be a restaurant's sale), so every business message goes to the router.
    Never for a WhatsApp swipe-reply: its meaning depends on the quoted message
    ("200000" replying to "¿gasto o ingreso?").
    """
    if (profile or {}).get("type", "personal") != "personal" or replied_to:
        return None
    if not text or _has_pending_question(conversation):
        return None

    normalized = normalize(text)
    tokens = _tokens(normalized)
    if _is_guarded(text, normalized, tokens):
        return None

    if _is_fast_report(normalized, tokens):
        return "reporting"

    has_amount = any(t[0].isdigit() for t in tokens)
    if not has_amount:
        return None
    if is_income_request(text):
        return "incomes"
    if len(tokens) <= _MAX_SIMPLE_EXPENSE_WORDS and any(
        t in _EXPENSE_WORDS for t in tokens
    ):
        return "expenses"
    return None
