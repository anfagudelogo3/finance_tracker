import logging

from finance_tracker.agents import expenses, incomes, reporting
from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.routing import router_agent
from finance_tracker.routing.fast_paths import fast_route

logger = logging.getLogger(__name__)


def handle_message(request: AgentRequest) -> AgentResponse:
    """Hybrid routing: a free keyword fast path when the intent is unambiguous,
    otherwise the router agent (one Claude call or more, see router_agent.py).

    fast_route never fires for business profiles, when the last assistant turn asked
    a question, or on multi-intent / suspicious text — those always reach the router.
    handler.py's is_excel_request still runs before this is ever called.
    """
    agent = fast_route(request.text, request.profile, request.conversation)
    if agent:
        logger.info("Routing via fast path → %s", agent)
        agent_module = {
            "expenses": expenses,
            "incomes": incomes,
            "reporting": reporting,
        }
        return agent_module[agent].handle(request)
    logger.info("Routing via router agent")
    return router_agent.handle(request)
