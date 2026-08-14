import expense_agent
import income_agent
from agent_types import AgentRequest, AgentResponse
from parser import _normalize

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

    Deterministic keyword signal first, mirroring handler.py's excel/report gates —
    handler.py's is_excel_request/is_report_request still run before this is ever
    called, so this only ever sees expense-or-income-shaped messages.
    """
    if is_income_request(request.text):
        return income_agent.handle(request)
    return expense_agent.handle(request)
