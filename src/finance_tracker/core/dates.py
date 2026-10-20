"""Record dates mentioned in a message ("ayer gasté 10 mil").

The expense and income agents ask Claude for two things: the exact words that say when
the money moved (`date_text`: "ayer", "el 28", "el 3 de octubre") and its own reading of
the date (`date`, YYYY-MM-DD). Code computes the date from the words for the common forms
— the model's day arithmetic isn't reliable ("el 28" on the 22nd is last month, "el 3"
is this month) — and only falls back to the model's date for anything else ("la semana
pasada"). Then: no date → the message's own date; a date after the message → not
recorded (it hasn't happened yet). The ingest time is separate: created_at, set by the
database. All dates are America/Bogota.
"""

import calendar
import re
from datetime import date, timedelta

from finance_tracker.core.text import normalize

_WEEKDAYS_ES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]
_WEEKDAY_INDEX = {normalize(name): i for i, name in enumerate(_WEEKDAYS_ES)}
_MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ],
        start=1,
    )
}
_MONTHS["setiembre"] = 9
_DAYS_AGO = {"hoy": 0, "ayer": 1, "anteayer": 2, "antier": 2, "antes de ayer": 2}

DATE_RULE = (
    '- date_text: the exact words of the message that say when it happened ("ayer", '
    '"anteayer", "el lunes", "el viernes pasado", "el 28", "el 3 de octubre", '
    '"hace 3 días"), or null if the message doesn\'t say when.\n'
    "- date: that day as YYYY-MM-DD, or null. Past money is never dated after today: a "
    "weekday means the most recent one, and a day number without a month that is greater "
    'than today\'s day is in the previous month. Only explicit future words ("mañana") '
    "give a future date."
)


def today_line(today: date) -> str:
    """The prompt line that lets the model turn "ayer" / "el lunes" into a date."""
    return f"Today is {_WEEKDAYS_ES[today.weekday()]} {today.isoformat()} (America/Bogota)."


def resolve_record_date(
    value: str | None, message_date: date, date_text: str | None = None
) -> tuple[date, bool]:
    """Return (record date, is_future) for one extracted record.

    The words in `date_text` win when code understands them; otherwise the model's
    `value`. None or an unreadable date falls back to the message's own date. A date
    after the message isn't something that already happened: the caller doesn't record it.
    """
    parsed = parse_date_text(date_text, message_date)
    if parsed is None and value:
        try:
            parsed = date.fromisoformat(str(value)[:10])
        except ValueError:
            parsed = None
    if parsed is None:
        return message_date, False
    return parsed, parsed > message_date


def parse_date_text(text: str | None, today: date) -> date | None:
    """Deterministic dates for the common Spanish forms, relative to `today`; None when
    the words aren't one of them. Days and weekdays without a month or year are the most
    recent one not after today."""
    if not text:
        return None
    words = " ".join(re.sub(r"[^\w/\s]", " ", normalize(text)).split())
    words = re.sub(r"^(el|la|este|esta|del|desde)\s+", "", words)

    if words in _DAYS_AGO:
        return today - timedelta(days=_DAYS_AGO[words])

    match = re.fullmatch(r"hace (\d{1,3}|un|una) dias?", words)
    if match:
        count = match.group(1)
        return today - timedelta(days=1 if count in ("un", "una") else int(count))

    match = re.fullmatch(r"(\w+?)( pasado)?", words)
    if match and match.group(1) in _WEEKDAY_INDEX:
        back = (today.weekday() - _WEEKDAY_INDEX[match.group(1)]) % 7
        return today - timedelta(days=back)

    match = re.fullmatch(r"(\d{1,2})", words)
    if match:
        return _recent_day(int(match.group(1)), today)

    match = re.fullmatch(r"(\d{1,2}) de (\w+)(?: (?:de |del )?(\d{4}))?", words)
    if match and match.group(2) in _MONTHS:
        return _recent_date(
            int(match.group(1)), _MONTHS[match.group(2)], match.group(3), today
        )

    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", words)
    if match:
        year = match.group(3)
        if year and len(year) == 2:
            year = f"20{year}"
        return _recent_date(int(match.group(1)), int(match.group(2)), year, today)
    return None


def _recent_day(day: int, today: date) -> date | None:
    """Day of month without a month: this month if not after today, else last month."""
    year, month = today.year, today.month
    if day > today.day:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return date(year, month, day)


def _recent_date(day: int, month: int, year: str | None, today: date) -> date | None:
    """Day and month (and maybe year): without a year, the most recent one not after
    today."""
    if not 1 <= month <= 12:
        return None
    candidate_year = int(year) if year else today.year
    try:
        candidate = date(candidate_year, month, day)
    except ValueError:
        return None
    if not year and candidate > today:
        try:
            candidate = date(candidate_year - 1, month, day)
        except ValueError:
            return None
    return candidate
