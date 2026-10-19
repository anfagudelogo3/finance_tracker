"""Deterministic keyword checks used for free routing (no LLM call).

Excel and report checks run in handler.py before the orchestrator; the income check
runs inside orchestrator.handle_message. Moved here from the old parser.py and orchestrator.py
so every keyword signal lives in one module.
"""

import logging
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
