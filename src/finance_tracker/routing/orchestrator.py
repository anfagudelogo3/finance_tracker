from finance_tracker.agents import expenses, incomes, reporting
from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.routing.fast_paths import is_income_request, is_report_request


def handle_message(request: AgentRequest) -> AgentResponse:
    """Dispatch to the matching specialist agent.

    Deterministic keyword signals only, checked in the same priority order handler.py
    used to apply inline: report, then income, then the expense default. Both checks
    live in routing/fast_paths.py (`is_report_request` is also still called directly by
    handler.py's report branch). handler.py's is_excel_request still runs before this is
    ever called.
    """
    if is_report_request(request.text):
        return reporting.handle(request)
    if is_income_request(request.text):
        return incomes.handle(request)
    return expenses.handle(request)
