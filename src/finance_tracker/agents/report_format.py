import logging
from collections import defaultdict
from datetime import date

logger = logging.getLogger(__name__)

_MONTHS_ES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic",
}


def _fmt_date(date_str: str) -> str:
    """Format an ISO date string as '4 abr 2026'."""
    d = date.fromisoformat(str(date_str))
    return f"{d.day} {_MONTHS_ES[d.month]} {d.year}"


def _fmt_amount(amount) -> str:
    """Format a number with thousands separator using dots (Colombian style)."""
    return f"{float(amount):,.0f}".replace(",", ".")


def format_report(expenses: list[dict], min_date: str, max_date: str) -> str:
    """Build a WhatsApp-friendly spending report grouped by currency then category.

    Currencies are never merged. Each currency gets its own section.
    """
    if not expenses:
        return (
            f"📊 Sin gastos registrados entre {_fmt_date(min_date)} "
            f"y {_fmt_date(max_date)}."
        )

    # Structure: {currency: {category: [amounts]}}
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in expenses:
        currency = str(row.get("currency", "COP")).upper()
        category = str(row.get("category", "otro"))
        amount = float(row["amount"])
        groups[currency][category].append(amount)

    logger.info(
        "Formatting report: %d expenses, %d currency(-ies), period %s to %s",
        len(expenses), len(groups), min_date, max_date,
    )

    header = f"📊 Resumen {_fmt_date(min_date)} – {_fmt_date(max_date)}:"
    lines = [header]

    for currency in sorted(groups):
        categories = groups[currency]
        currency_total = sum(sum(amts) for amts in categories.values())
        currency_count = sum(len(amts) for amts in categories.values())

        if len(groups) > 1:
            # Multiple currencies: add a sub-header per currency
            lines.append(f"\n{currency} ({currency_count} {'gasto' if currency_count == 1 else 'gastos'}):")
        else:
            lines.append("")

        for category in sorted(categories):
            amts = categories[category]
            cat_total = sum(amts)
            count = len(amts)
            label = f"{'gasto' if count == 1 else 'gastos'}"
            lines.append(
                f"  {category}: {currency} {_fmt_amount(cat_total)} ({count} {label})"
            )

        lines.append(
            f"Total: {currency} {_fmt_amount(currency_total)} — "
            f"{currency_count} {'gasto' if currency_count == 1 else 'gastos'}"
        )

    return "\n".join(lines)
