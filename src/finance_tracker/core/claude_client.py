import anthropic

from finance_tracker.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_TIMEOUT_SECONDS,
)

# The SDK's defaults (10 min, 2 retries) could outlive the Lambda; calls made while a
# message's deadline is set pass a shorter timeout (core/deadline.call_options)
client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=CLAUDE_TIMEOUT_SECONDS,
    max_retries=CLAUDE_MAX_RETRIES,
)
