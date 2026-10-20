from datetime import date

from finance_tracker.core.dates import resolve_record_date, today_line

MESSAGE_DATE = date(2026, 10, 22)  # a Thursday


def test_no_date_means_the_messages_date():
    assert resolve_record_date(None, MESSAGE_DATE) == (MESSAGE_DATE, False)


def test_past_date_is_used():
    assert resolve_record_date("2026-10-21", MESSAGE_DATE) == (
        date(2026, 10, 21),
        False,
    )


def test_same_day_is_not_future():
    assert resolve_record_date("2026-10-22", MESSAGE_DATE) == (MESSAGE_DATE, False)


def test_future_date_is_flagged():
    assert resolve_record_date("2026-10-23", MESSAGE_DATE) == (date(2026, 10, 23), True)


def test_garbage_falls_back_to_the_messages_date():
    assert resolve_record_date("ayer", MESSAGE_DATE) == (MESSAGE_DATE, False)


def test_old_dates_are_kept():
    """No 'too old' rejection: a confirmed old date could never get through it."""
    assert resolve_record_date("2025-01-10", MESSAGE_DATE) == (date(2025, 1, 10), False)


def test_today_line_names_the_weekday():
    assert today_line(MESSAGE_DATE) == "Today is jueves 2026-10-22 (America/Bogota)."


import pytest

from finance_tracker.core.dates import parse_date_text


@pytest.mark.parametrize(
    "words,expected",
    [
        ("hoy", "2026-10-22"),
        ("ayer", "2026-10-21"),
        ("Anteayer", "2026-10-20"),
        ("antier", "2026-10-20"),
        ("hace 3 días", "2026-10-19"),
        ("hace un día", "2026-10-21"),
        ("el lunes", "2026-10-19"),
        ("el jueves", "2026-10-22"),  # today is Thursday
        ("el viernes pasado", "2026-10-16"),
        ("el miércoles", "2026-10-21"),
        ("el 3", "2026-10-03"),  # not after today: this month
        ("el 22", "2026-10-22"),
        ("el 28", "2026-09-28"),  # after today: last month
        ("el 31", None),  # September has no 31st
        ("el 3 de octubre", "2026-10-03"),
        ("28 de septiembre", "2026-09-28"),
        ("el 15 de diciembre", "2025-12-15"),  # no year: the most recent one
        ("el 15 de diciembre de 2026", "2026-12-15"),  # explicit year kept (future)
        ("15/10", "2026-10-15"),
        ("15/10/2025", "2025-10-15"),
        ("la semana pasada", None),  # left to the model
        (None, None),
    ],
)
def test_parse_date_text(words, expected):
    result = parse_date_text(words, MESSAGE_DATE)
    assert (result.isoformat() if result else None) == expected


def test_words_win_over_the_models_date():
    """'el 28' on the 22nd: the model said Oct 28 (future); the words say Sep 28."""
    assert resolve_record_date("2026-10-28", MESSAGE_DATE, "el 28") == (
        date(2026, 9, 28),
        False,
    )


def test_model_date_used_when_words_are_unknown():
    assert resolve_record_date("2026-10-14", MESSAGE_DATE, "la semana pasada") == (
        date(2026, 10, 14),
        False,
    )


def test_manana_stays_future():
    assert resolve_record_date("2026-10-23", MESSAGE_DATE, "mañana")[1] is True
