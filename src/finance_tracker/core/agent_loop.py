"""Generic Claude tool-use loop shared by multi-step agents (the router first).

The model's stop_reason is the stopping mechanism: the loop keeps going while Claude
asks for tools (`tool_use`) and finishes normally on `end_turn`, so a multi-step task
is never cut off midway. max_iterations and the message's deadline (core/deadline.py)
are safeguards for a loop that isn't converging, not the expected way out. See
docs/agent-roadmap.md §4.3.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from finance_tracker.config import CLAUDE_MIN_CALL_SECONDS
from finance_tracker.core import deadline

logger = logging.getLogger(__name__)

# Possible LoopOutcome.status values
END_TURN = "end_turn"
MAX_ITERATIONS = "max_iterations"
TIMEOUT = "timeout"
MAX_TOKENS = "max_tokens"
REFUSAL = "refusal"


@dataclass
class ToolCall:
    name: str
    input: dict
    result: str
    is_error: bool


@dataclass
class LoopOutcome:
    status: str  # END_TURN | MAX_ITERATIONS | TIMEOUT | MAX_TOKENS | REFUSAL | "unexpected:<reason>"
    final_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    iterations: int = 0


def run_tool_loop(
    *,
    client,
    model: str,
    system: str,
    tools: list[dict],
    messages: list[dict],
    execute_tool: Callable[[str, dict], str],
    max_iterations: int,
    max_tokens: int,
    output_config: dict | None = None,
) -> LoopOutcome:
    """Run Claude with `tools` until it ends its turn (or a safeguard trips).

    `execute_tool(name, input)` returns the tool_result text. If it raises, the error
    goes back to Claude as an `is_error` tool_result and the loop continues, so one
    failing tool doesn't abort the others. `messages` is extended in place, append-only:
    assistant content is sent back exactly as received (thinking blocks included).
    """
    outcome = LoopOutcome(status=MAX_ITERATIONS)
    extra = {"output_config": output_config} if output_config else {}

    for iteration in range(1, max_iterations + 1):
        left = deadline.remaining()
        if left is not None and left < CLAUDE_MIN_CALL_SECONDS:
            logger.warning(
                "Agent loop out of time after %d iteration(s) (%.1f s left)",
                iteration - 1,
                left,
            )
            outcome.status = TIMEOUT
            return outcome

        started = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "auto"},
            messages=messages,
            **extra,
            **deadline.call_options(),
        )
        outcome.iterations = iteration
        usage = getattr(response, "usage", None)
        logger.info(
            "Agent loop model=%s iteration=%d stop_reason=%s seconds=%.2f "
            "input_tokens=%s output_tokens=%s",
            model,
            iteration,
            response.stop_reason,
            time.monotonic() - started,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )

        if response.stop_reason == "end_turn":
            outcome.status = END_TURN
            outcome.final_text = _text_of(response)
            return outcome

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                call = _execute(block, execute_tool)
                outcome.tool_calls.append(call)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": call.result,
                        "is_error": call.is_error,
                    }
                )
            # All results for this turn go back in ONE user message
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "max_tokens":
            # A tool_use cut off by max_tokens may have truncated input — never run it
            logger.warning("Agent loop hit max_tokens at iteration %d", iteration)
            outcome.status = MAX_TOKENS
            return outcome

        if response.stop_reason == "refusal":
            logger.warning(
                "Agent loop refusal: %s", getattr(response, "stop_details", None)
            )
            outcome.status = REFUSAL
            return outcome

        logger.warning("Agent loop unexpected stop_reason=%s", response.stop_reason)
        outcome.status = f"unexpected:{response.stop_reason}"
        return outcome

    logger.warning(
        "Agent loop reached max_iterations=%d without end_turn; tool calls: %s",
        max_iterations,
        [c.name for c in outcome.tool_calls],
    )
    return outcome


def _execute(block, execute_tool) -> ToolCall:
    tool_input = dict(block.input or {})
    try:
        return ToolCall(
            block.name, tool_input, execute_tool(block.name, tool_input), False
        )
    except Exception as exc:  # reported to Claude, never raised past the loop
        logger.exception("Tool %s failed", block.name)
        return ToolCall(block.name, tool_input, json.dumps({"error": str(exc)}), True)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()
