import os

# Twilio
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]  # e.g. whatsapp:+14155238886
ALLOWED_PHONE_NUMBERS = set(os.environ["ALLOWED_PHONE_NUMBERS"].split(","))  # e.g. +573001234567,+573009876543

# OpenAI
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

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
