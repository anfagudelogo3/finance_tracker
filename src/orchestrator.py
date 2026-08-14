import expense_agent
import income_agent
import reporting_agent
from agent_types import AgentRequest, AgentResponse
from parser import _normalize, is_report_request

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
    through to expense_agent (same as before this agent existed, not a regression). A
    real classification call is deferred until ambiguous cases actually show up.
    """
    normalized = _normalize(text)
    return any(kw in normalized for kw in _INCOME_KEYWORDS_PHRASE)


def handle_message(request: AgentRequest) -> AgentResponse:
    """Dispatch to the matching specialist agent.

    Deterministic keyword signals only, checked in the same priority order handler.py
    used to apply inline: report, then income, then the expense default. `is_report_request`
    is reused unchanged from parser.py (it's also still called directly by handler.py's
    Excel branch, which stays on the old path — see docs/agent-architecture.md Phase 3).
    handler.py's is_excel_request still runs before this is ever called.
    """
    if is_report_request(request.text):
        return reporting_agent.handle(request)
    if is_income_request(request.text):
        return income_agent.handle(request)
    return expense_agent.handle(request)
