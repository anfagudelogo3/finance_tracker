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

# User profile (user_prefs → profile) used when a user has none set
DEFAULT_PROFILE = {"type": "personal"}
DEFAULT_CURRENCY = "COP"

# Every date and timestamp the app produces is in this zone (code and DB session)
TIMEZONE = "America/Bogota"

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
# Business profiles get their own fixed defaults (seeded by onboarding, Phase 6b)
DEFAULT_BUSINESS_EXPENSE_CATEGORIES = [
    "insumos", "nómina", "arriendo", "servicios públicos", "impuestos",
    "marketing", "transporte", "mantenimiento", "otro",
]
DEFAULT_BUSINESS_INCOME_CATEGORIES = ["ventas", "servicios", "otro"]

# Short explanations shown next to the default categories during onboarding
CATEGORY_HINTS = {
    "comida": "restaurantes, almuerzos, domicilios",
    "transporte": "bus, taxi, Uber, gasolina",
    "mercado": "supermercado y compras de la casa",
    "salud": "médico, medicamentos, seguros",
    "entretenimiento": "cine, salidas, suscripciones",
    "hogar": "arriendo, reparaciones, cosas para la casa",
    "educacion": "cursos, libros, matrículas",
    "ropa": "ropa y calzado",
    "servicios": "luz, agua, internet, celular",
    "salario": "tu sueldo o quincena",
    "freelance": "pagos por trabajos independientes",
    "negocio": "ingresos de un negocio propio",
    "inversion": "intereses, dividendos, rendimientos",
    "reembolso": "dinero que te devuelven",
    "regalo": "dinero que te regalan",
    "insumos": "materia prima y productos para vender",
    "nómina": "salarios y pagos a empleados",
    "arriendo": "local, bodega u oficina",
    "servicios públicos": "luz, agua, gas, internet",
    "impuestos": "IVA, renta, industria y comercio",
    "marketing": "publicidad y redes sociales",
    "mantenimiento": "reparaciones y equipos",
    "ventas": "lo que vendes",
    "otro": "todo lo que no encaje en las demás",
}

# Onboarding (Phase 6b) — see docs/design/phase-6-users-onboarding.md
ONBOARDING_ABANDON_HOURS = 24      # an unfinished onboarding older than this starts over
ONBOARDING_MAX_CATEGORIES = 25     # per kind
ONBOARDING_CURRENCY_OPTIONS = ["COP", "USD", "EUR"]
KEYWORD_SKIP = "omitir"
KEYWORD_SKIP_ALL = "omitir todo"
KEYWORD_RESTART = "reiniciar configuracion"   # compared accent-insensitively, whole message

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

# Onboarding messages (Phase 6b). {placeholders} are filled in agents/onboarding.py.
MSG_ONBOARDING_WELCOME = (
    "👋 ¡Hola! Soy tu asistente financiero personal, aquí mismo en WhatsApp.\n\n"
    "Olvídate de hojas de cálculo y apps complicadas: solo escríbeme o mándame una nota de voz "
    "como le hablarías a un amigo —_\"almuerzo 25 mil\"_, _\"me pagaron la quincena\"_— y yo me "
    "encargo de organizarlo todo. 💸\n\n"
    "Con mi ayuda vas a:\n"
    "✅ Saber exactamente en qué se va tu dinero\n"
    "✅ Registrar gastos e ingresos en segundos, por texto, voz o foto del recibo\n"
    "✅ Pedir resúmenes cuando quieras: _\"¿cuánto gasté este mes?\"_\n"
    "✅ Descargar tus movimientos en Excel\n\n"
    "Tomar el control de tus finanzas nunca fue tan fácil. Antes de empezar, te haré unas "
    "preguntas rápidas para adaptarme a ti (menos de 2 minutos). 🚀"
)
MSG_ONBOARDING_RESTART = (
    "🔄 Vamos a configurar tu cuenta de nuevo. Tu tipo de perfil se mantiene; "
    "te preguntaré por tu moneda y tus categorías."
)
MSG_ONBOARDING_SKIP_HINT = "_(escribe *omitir* para saltar esta pregunta u *omitir todo* para terminar)_"
MSG_ONBOARDING_Q_PROFILE = (
    "¿Para qué vas a usar el asistente?\n\n"
    "1. *Personal* — tus finanzas del día a día: comida, transporte, servicios, tu salario…\n"
    "2. *Negocio* — las ventas y los gastos de tu emprendimiento o empresa: insumos, nómina, "
    "arriendo…\n\n"
    "Responde con el número."
)
MSG_ONBOARDING_Q_BUSINESS = (
    "🏪 Cuéntame de tu negocio en un solo mensaje: ¿cómo se llama, de qué es, qué vende y "
    "cuáles son sus gastos principales?\n\n"
    "Ej: _\"La Sazón de Ana, un restaurante. Vendo almuerzos y domicilios; mis gastos son "
    "insumos, nómina y arriendo.\"_"
)
MSG_ONBOARDING_Q_CURRENCY = (
    "💱 ¿Cuál es tu moneda principal? La usaré cuando no me digas la moneda de un gasto o "
    "ingreso.\n\n"
    "1. COP — peso colombiano\n"
    "2. USD — dólar\n"
    "3. EUR — euro\n\n"
    "O escríbeme otra (ej. MXN)."
)
MSG_ONBOARDING_Q_CATEGORIES = (
    "{icon} Para tus *{label}* uso estas categorías:\n{categories}\n\n"
    "1. Usar estas\n"
    "2. Personalizarlas\n\n"
    "También puedes decirme directamente qué cambiar, ej. _\"{example}\"_."
)
MSG_ONBOARDING_Q_CUSTOMIZE = (
    "¿Qué quieres cambiar en tus categorías de {label}? Puedes agregar, quitar o renombrar, "
    "ej. _\"{example}\"_."
)
MSG_ONBOARDING_CATEGORIES_SET = "✅ Tus categorías de {label}: {categories}."
MSG_ONBOARDING_REMINDER = "Para empezar necesito terminar tu configuración (o escribe *omitir*)."
MSG_ONBOARDING_DONE = (
    "🎉 ¡Listo! Tu cuenta quedó configurada:\n"
    "• Perfil: {profile}\n"
    "• Moneda: {currency}\n"
    "• Gastos: {expense_categories}\n"
    "• Ingresos: {income_categories}\n\n"
    "Ya puedes escribirme tus gastos e ingresos. Si quieres cambiar algo, solo dímelo "
    "(ej. _\"agrega la categoría mascotas\"_) o escribe *reiniciar configuración*."
)
MSG_ONBOARDING_HELD = "Antes de empezar me escribiste: _\"{text}\"_. ¿Lo registro?\n\n1. Sí\n2. No"
MSG_ONBOARDING_HELD_IMAGE = "Antes de empezar me enviaste una foto: envíamela de nuevo para registrarla."
MSG_ONBOARDING_HELD_DISCARDED = "Listo, no lo registré."
