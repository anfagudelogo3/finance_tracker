import logging
from dataclasses import replace

from finance_tracker.agents import expenses, incomes, records, reporting
from finance_tracker.config import MSG_ERROR
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

    A move between expenses and incomes left half done (the record removed but never
    re-created) is settled here, after every agent ran: see records.settle_moves.
    """
    records.start_message()
    agent = fast_route(
        request.text, request.profile, request.conversation, request.replied_to
    )
    if agent:
        logger.info("Routing via fast path → %s", agent)
        agent_module = {
            "expenses": expenses,
            "incomes": incomes,
            "reporting": reporting,
        }
        response = agent_module[agent].handle(request)
    else:
        logger.info("Routing via router agent")
        response = router_agent.handle(request)

    unfinished = records.settle_moves(request.user_id, request.message_id)
    text = "\n\n".join(p for p in (response.reply_text, *unfinished) if p)
    if text != response.reply_text or not text:
        response = replace(response, reply_text=text or MSG_ERROR)
    return response
