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


def format_confirmation(expenses: list[dict]) -> str:
    """Build a human-friendly confirmation message in Spanish."""
    if len(expenses) == 1:
        expense = expenses[0]
        currency = expense.get("currency", "COP")
        amount = f"{expense['amount']:,.0f}".replace(",", ".")
        category = expense.get("category", "otro")
        expense_date = expense.get("date", "hoy")
        msg = f"✅ Registré {currency} {amount} en {category} para {expense_date}"
        if expense.get("payment_method"):
            msg += f" ({expense['payment_method']})"
        return msg

    # Multiple expenses: bullet list + total if same currency
    lines = [f"✅ Registré {len(expenses)} gastos:"]
    totals: dict[str, float] = {}
    for expense in expenses:
        currency = expense.get("currency", "COP")
        amount = expense["amount"]
        category = expense.get("category", "otro")
        formatted = f"{amount:,.0f}".replace(",", ".")
        lines.append(f"  • {currency} {formatted} en {category}")
        totals[currency] = totals.get(currency, 0) + amount

    if len(totals) == 1:
        currency, total = next(iter(totals.items()))
        formatted_total = f"{total:,.0f}".replace(",", ".")
        lines.append(f"Total: {currency} {formatted_total}")

    return "\n".join(lines)
