import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def setup_tracing() -> bool:
    """Set up Phoenix OpenTelemetry tracing for all OpenAI calls.

    Enabled when PHOENIX_TRACING=1 or PHOENIX_COLLECTOR_ENDPOINT is set.
    Idempotent — safe to call multiple times. Fails gracefully when
    arize-phoenix is not installed (e.g. in the production Lambda package).

    Returns True if tracing was successfully enabled, False otherwise.
    """
    global _initialized
    if _initialized:
        return True

    enabled = (
        os.environ.get("PHOENIX_TRACING", "0") == "1"
        or bool(os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"))
    )
    if not enabled:
        return False

    try:
        from phoenix.otel import register
        from openinference.instrumentation.openai import OpenAIInstrumentor

        endpoint = os.environ.get(
            "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
        )
        tracer_provider = register(
            project_name="finance-tracker",
            endpoint=endpoint,
        )
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

        _initialized = True
        logger.info("Phoenix tracing enabled → %s", endpoint)
        return True

    except ImportError:
        logger.warning(
            "PHOENIX_TRACING is set but arize-phoenix is not installed. "
            "Run: uv sync --group evals"
        )
        return False
    except Exception:
        logger.warning("Failed to initialise Phoenix tracing", exc_info=True)
        return False
