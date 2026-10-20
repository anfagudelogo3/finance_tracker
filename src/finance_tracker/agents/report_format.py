import logging
from collections import defaultdict
from datetime import date

logger = logging.getLogger(__name__)

_MONTHS_ES = {
    1: "ene",
    2: "feb",
    3: "mar",
    4: "abr",
    5: "may",
    6: "jun",
    7: "jul",
    8: "ago",
    9: "sep",
    10: "oct",
    11: "nov",
    12: "dic",
}


def _fmt_date(date_str: str) -> str:
    """Format an ISO date string as '4 abr 2026'."""
    d = date.fromisoformat(str(date_str))
    return f"{d.day} {_MONTHS_ES[d.month]} {d.year}"


def _fmt_amount(amount) -> str:
    """Format a number with thousands separator using dots (Colombian style)."""
    return f"{float(amount):,.0f}".replace(",", ".")


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _group(rows: list[dict]) -> dict[str, dict[str, list[float]]]:
    """{currency: {category: [amounts]}}"""
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        currency = str(row.get("currency", "COP")).upper()
        groups[currency][str(row.get("category", "otro"))].append(float(row["amount"]))
    return groups


def _section(
    icon: str,
    title: str,
    categories: dict[str, list[float]],
    currency: str,
    singular: str,
    plural: str,
) -> list[str]:
    if not categories:
        return [f"{icon} {title}: ninguno"]
    lines = [f"{icon} {title}"]
    for category in sorted(categories):
        amounts = categories[category]
        lines.append(
            f"  {category}: {currency} {_fmt_amount(sum(amounts))} "
            f"({_count(len(amounts), singular, plural)})"
        )
    total = sum(sum(a) for a in categories.values())
    count = sum(len(a) for a in categories.values())
    lines.append(
        f"  Total: {currency} {_fmt_amount(total)} — {_count(count, singular, plural)}"
    )
    return lines


def format_report(
    expenses: list[dict], incomes: list[dict], min_date: str, max_date: str
) -> str:
    """Build a WhatsApp-friendly report: expenses, then incomes, then the net.

    Grouped by currency, then category. Currencies are never merged: each one gets its
    own block, with its own net (incomes − expenses).
    """
    if not expenses and not incomes:
        return (
            f"📊 Sin movimientos registrados entre {_fmt_date(min_date)} "
            f"y {_fmt_date(max_date)}."
        )

    spent = _group(expenses)
    received = _group(incomes)
    currencies = sorted(set(spent) | set(received))
    logger.info(
        "Formatting report: %d expenses, %d incomes, %d currency(-ies), period %s to %s",
        len(expenses),
        len(incomes),
        len(currencies),
        min_date,
        max_date,
    )

    lines = [f"📊 Resumen {_fmt_date(min_date)} – {_fmt_date(max_date)}:"]
    for currency in currencies:
        lines.append("")
        if len(currencies) > 1:
            lines.append(f"*{currency}*")
        lines += _section(
            "💸", "Gastos", spent.get(currency, {}), currency, "gasto", "gastos"
        )
        lines += _section(
            "💰",
            "Ingresos",
            received.get(currency, {}),
            currency,
            "ingreso",
            "ingresos",
        )
        net = sum(sum(a) for a in received.get(currency, {}).values()) - sum(
            sum(a) for a in spent.get(currency, {}).values()
        )
        sign = "-" if net < 0 else ""
        lines.append(
            f"{'📉' if net < 0 else '📈'} Neto: {currency} {sign}{_fmt_amount(abs(net))}"
        )
    return "\n".join(lines)
