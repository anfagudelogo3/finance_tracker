import logging

from twilio.rest import Client

from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER

logger = logging.getLogger(__name__)
_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_message(to: str, text: str) -> str:
    """Send a WhatsApp message via Twilio. Returns the message SID."""
    logger.info("Sending WhatsApp message to %s", to)
    message = _client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=text,
        to=f"whatsapp:{to}",
    )
    logger.debug("Twilio message created with SID: %s", message.sid)
    return message.sid


def format_confirmation(expense: dict) -> str:
    """Build a human-friendly confirmation message in Spanish."""
    amount = f"{expense['amount']:,.0f}".replace(",", ".")
    category = expense.get("category", "otro")
    date = expense.get("date", "hoy")

    msg = f"✅ Registré COP {amount} en {category} para {date}"

    if expense.get("payment_method"):
        msg += f" ({expense['payment_method']})"

    return msg
