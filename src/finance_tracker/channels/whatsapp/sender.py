import logging

from twilio.rest import Client

from finance_tracker.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER

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


def send_document(to: str, media_url: str, filename: str) -> str:
    """Send a WhatsApp document (file) via Twilio. Returns the message SID."""
    logger.info("Sending document '%s' to %s", filename, to)
    message = _client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        media_url=[media_url],
        body=filename,
        to=f"whatsapp:{to}",
    )
    logger.debug("Twilio document message created with SID: %s", message.sid)
    return message.sid


def format_confirmation(expenses: list[dict]) -> str:
    """Build a human-friendly confirmation message in Spanish.

    Says "Gasto" explicitly (💸) so it can't be confused with an income confirmation
    (💰) when the router sends both in one reply.
    """
    if len(expenses) == 1:
        expense = expenses[0]
        currency = expense.get("currency", "COP")
        amount = f"{expense['amount']:,.0f}".replace(",", ".")
        category = expense.get("category", "otro")
        expense_date = expense.get("date", "hoy")
        details = expense_date
        if expense.get("payment_method"):
            details += f", {expense['payment_method']}"
        return f"💸 Gasto registrado: {currency} {amount} en {category} ({details})"

    # Multiple expenses: bullet list + total if same currency
    lines = [f"💸 Registré {len(expenses)} gastos:"]
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


def format_income_confirmation(incomes: list[dict]) -> str:
    """Build a human-friendly income confirmation message in Spanish.

    Mirrors format_confirmation's shape — no payment_method, since incomes has no
    such column; "de {category}" instead of "en {category}" (received AS/FROM a
    category, not spent ON one); "Ingreso" and 💰 so it can't be read as an expense.
    """
    if len(incomes) == 1:
        income = incomes[0]
        currency = income.get("currency", "COP")
        amount = f"{income['amount']:,.0f}".replace(",", ".")
        category = income.get("category", "otro")
        income_date = income.get("date", "hoy")
        return f"💰 Ingreso registrado: {currency} {amount} de {category} ({income_date})"

    # Multiple incomes: bullet list + total if same currency
    lines = [f"💰 Registré {len(incomes)} ingresos:"]
    totals: dict[str, float] = {}
    for income in incomes:
        currency = income.get("currency", "COP")
        amount = income["amount"]
        category = income.get("category", "otro")
        formatted = f"{amount:,.0f}".replace(",", ".")
        lines.append(f"  • {currency} {formatted} de {category}")
        totals[currency] = totals.get(currency, 0) + amount

    if len(totals) == 1:
        currency, total = next(iter(totals.items()))
        formatted_total = f"{total:,.0f}".replace(",", ".")
        lines.append(f"Total: {currency} {formatted_total}")

    return "\n".join(lines)
