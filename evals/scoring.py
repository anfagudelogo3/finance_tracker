"""Pure, I/O-free scoring functions for LLM eval metrics.

No network calls, no LLM calls, no file I/O — fully testable offline.
"""
import unicodedata


def _normalize(value) -> str | None:
    """Lowercase, strip accents, and trim whitespace for fuzzy-insensitive comparison."""
    if value is None:
        return None
    text = str(value).strip()
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode().strip()


def score_expense(actual: list[dict], expected: dict) -> dict:
    """Score parse_expense output against a dataset entry.

    Args:
        actual:   List of expense dicts returned by parse_expense().
        expected: Dataset 'expected' dict with 'count' and 'expenses' list.
                  Only fields present in each expected expense are scored;
                  omitted fields are skipped entirely.

    Returns:
        {
            "all_correct": bool,
            "count_correct": bool,
            "expenses": [           # populated only when count matches
                {
                    "all_correct": bool,
                    "fields": {
                        "<field>": {"expected": ..., "actual": ..., "correct": bool}
                    }
                },
                ...
            ]
        }
    """
    expected_count = expected.get("count", len(expected.get("expenses", [])))
    count_correct = len(actual) == expected_count

    if not count_correct:
        return {"all_correct": False, "count_correct": False, "expenses": []}

    expense_scores = [
        _score_single_expense(act, exp)
        for act, exp in zip(actual, expected.get("expenses", []))
    ]
    all_correct = all(e["all_correct"] for e in expense_scores)

    return {
        "all_correct": all_correct,
        "count_correct": True,
        "expenses": expense_scores,
    }


def _score_single_expense(actual: dict, expected: dict) -> dict:
    fields: dict[str, dict] = {}
    all_correct = True

    for field in ("amount", "currency", "category", "payment_method", "merchant"):
        if field not in expected:
            continue

        exp_val = expected[field]
        act_val = actual.get(field)

        if isinstance(exp_val, (int, float)):
            try:
                correct = float(act_val) == float(exp_val)
            except (TypeError, ValueError):
                correct = False
        else:
            correct = _normalize(act_val) == _normalize(exp_val)

        fields[field] = {"expected": exp_val, "actual": act_val, "correct": correct}
        if not correct:
            all_correct = False

    return {"all_correct": all_correct, "fields": fields}


def score_date_range(actual: dict, expected: dict) -> dict:
    """Score parse_report_request output against a dataset entry.

    Args:
        actual:   {"min_date": "YYYY-MM-DD", "max_date": "YYYY-MM-DD"}
        expected: Same shape from the dataset.

    Returns:
        {
            "all_correct": bool,
            "min_date": {"expected": ..., "actual": ..., "correct": bool},
            "max_date": {"expected": ..., "actual": ..., "correct": bool},
        }
    """
    result: dict = {}
    all_correct = True

    for field in ("min_date", "max_date"):
        exp_val = expected.get(field)
        act_val = actual.get(field)
        correct = act_val == exp_val
        result[field] = {"expected": exp_val, "actual": act_val, "correct": correct}
        if not correct:
            all_correct = False

    return {"all_correct": all_correct, **result}
