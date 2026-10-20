"""One time budget per inbound message, shared by every Claude call it makes.

The handler starts it from the Lambda's remaining time minus a reserve for sending the
reply, so nested loops (router → specialist) draw from the same clock instead of
stacking fixed timeouts, and a slow call can't run past the Lambda's own timeout (which
would end the request with no reply at all). Unset — tests, evals — means no limit.
"""

import time
from contextvars import ContextVar
from typing import Callable

from finance_tracker.config import CLAUDE_MAX_RETRIES

_deadline: ContextVar[tuple[float, Callable[[], float]] | None] = ContextVar(
    "deadline", default=None
)


def start(seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
    """Allow `seconds` from now for the rest of this message."""
    _deadline.set((clock() + seconds, clock))


def clear() -> None:
    _deadline.set(None)


def remaining() -> float | None:
    """Seconds left (negative once past), or None when no deadline is set."""
    current = _deadline.get()
    if current is None:
        return None
    end, clock = current
    return end - clock()


def call_options() -> dict:
    """Request options for one Claude call: a timeout that ends it, retries included,
    before the deadline. Empty when no deadline is set (the client's defaults apply)."""
    left = remaining()
    if left is None:
        return {}
    # The client retries a timed-out attempt, so each attempt gets its share
    return {"timeout": max(left, 1.0) / (CLAUDE_MAX_RETRIES + 1)}
