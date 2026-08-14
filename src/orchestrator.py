import expense_agent
from agent_types import AgentRequest, AgentResponse


def handle_message(request: AgentRequest) -> AgentResponse:
    """Dispatch to the matching specialist agent.

    Single-agent dispatch for now — expense is the only agent that exists.
    handler.py's existing is_excel_request/is_report_request keyword gates still
    run before this is ever called, so there's nothing to route between yet; a real
    routing/classification decision lands once income and report agents exist.
    """
    return expense_agent.handle(request)
