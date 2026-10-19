import os

from finance_tracker.core.ssm_config import load_ssm_parameters

# In the Lambda, secrets and settings come from SSM Parameter Store (CONFIG_SSM_PATH);
# loaded once per cold start, before anything below reads os.environ. No-op locally.
load_ssm_parameters()

# Twilio
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]  # e.g. whatsapp:+14155238886
ALLOWED_PHONE_NUMBERS = set(os.environ["ALLOWED_PHONE_NUMBERS"].split(","))  # e.g. +573001234567,+573009876543

# OpenAI
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Anthropic (Claude) — expense agent's text/audio extraction path
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Database (Neon PostgreSQL)
DATABASE_URL = os.environ["DATABASE_URL"]

# S3
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]

# Webhook
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # exact URL configured in Twilio sandbox settings

# OpenAI models
OPENAI_TEXT_MODEL = "gpt-4o-mini"
OPENAI_VISION_MODEL = "gpt-4o"
OPENAI_AUDIO_MODEL = "whisper-1"
OPENAI_AUDIO_LANGUAGE = "es"

# Claude models — expense agent (text/audio only; image expense stays on OpenAI vision)
CLAUDE_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

# Router agent (Phase 5) — runs only when no keyword fast path matched. Haiku 5.5 at low
# effort: cheapest current tier, thinks before choosing tools (tool_choice=auto).
CLAUDE_ROUTER_MODEL = "claude-haiku-5-5"
ROUTER_EFFORT = "low"
ROUTER_MAX_TOKENS = 2048          # thinking counts toward this on Haiku 5.5
ROUTER_MAX_ITERATIONS = 6         # safeguard only — the loop normally ends on end_turn
ROUTER_MAX_DELEGATIONS = 3        # specialist calls allowed per message
AGENT_LOOP_TIMEOUT_SECONDS = 12   # stays under Twilio's 15 s webhook timeout
ROUTER_MAX_INPUT_CHARS = 1000     # longer messages are truncated before routing

# User profile (users.prefs.profile) used when a user has none set
DEFAULT_PROFILE = {"type": "personal"}

# S3
PRESIGNED_URL_EXPIRY_SECONDS = 60

# Parser
FUZZY_MATCH_CUTOFF = 0.8

# Default categories, seeded for every new user (they can customize later)
DEFAULT_EXPENSE_CATEGORIES = [
    "comida", "transporte", "mercado", "salud", "entretenimiento",
    "hogar", "educacion", "ropa", "servicios", "otro",
]
DEFAULT_INCOME_CATEGORIES = [
    "salario", "freelance", "negocio", "inversion", "reembolso", "regalo", "otro",
]

# Conversation memory window (how much recent history the chatbot sees)
CONVERSATION_MAX_TURNS = 8          # cap on number of recent turns loaded
CONVERSATION_WINDOW_MINUTES = 30    # only turns within this window count as "recent"

# Reply messages
MSG_ERROR = "Ocurrió un error inesperado. Por favor intenta de nuevo."
MSG_EMPTY_EXPENSE = "No entendí ese mensaje. Intenta describir el gasto con monto y categoría."
MSG_EMPTY_INCOME = "No entendí ese mensaje. Intenta describir el ingreso con monto y categoría."
MSG_OUT_OF_SCOPE = (
    "Solo puedo ayudarte con tus finanzas: registrar gastos e ingresos y darte resúmenes. "
    "¿Qué quieres registrar o consultar?"
)
MSG_ROUTER_EMPTY = (
    "¿En qué te ayudo? Puedes registrar un gasto (\"almuerzo 20000\"), un ingreso "
    "(\"me pagaron 2000000\") o pedir un resumen (\"cuánto gasté este mes\")."
)
MSG_DELEGATION_FAILED = "⚠️ No pude procesar: {task}"
MSG_PARTIAL_TIMEOUT = "⚠️ No alcancé a terminar todo. Revisa lo registrado e intenta de nuevo con lo que faltó."
