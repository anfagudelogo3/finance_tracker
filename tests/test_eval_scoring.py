"""Offline unit tests for evals/scoring.py.

No LLM calls, no network — runs in the normal test suite (uv run pytest tests/).
These tests verify the scoring logic itself so the eval harness is trustworthy.
"""
import pytest

from evals.scoring import score_expense, score_date_range


# ── score_expense ──────────────────────────────────────────────────────────────

class TestScoreExpenseSingle:
    def _actual(self, **kwargs):
        base = {
            "amount": 32000,
            "currency": "COP",
            "category": "comida",
            "payment_method": None,
            "merchant": None,
            "description": "almuerzo",
        }
        base.update(kwargs)
        return [base]

    def _expected(self, **kwargs):
        base = {"count": 1, "expenses": [{"amount": 32000, "currency": "COP", "category": "comida", "payment_method": None}]}
        base["expenses"][0].update(kwargs)
        return base

    def test_all_fields_correct(self):
        result = score_expense(self._actual(), self._expected())
        assert result["all_correct"] is True
        assert result["count_correct"] is True

    def test_wrong_amount(self):
        result = score_expense(self._actual(amount=15000), self._expected())
        assert result["all_correct"] is False
        assert result["expenses"][0]["fields"]["amount"]["correct"] is False

    def test_wrong_category(self):
        result = score_expense(self._actual(category="transporte"), self._expected())
        assert result["expenses"][0]["fields"]["category"]["correct"] is False

    def test_wrong_payment_method(self):
        result = score_expense(self._actual(payment_method="tarjeta"), self._expected())
        assert result["expenses"][0]["fields"]["payment_method"]["correct"] is False

    def test_category_case_insensitive(self):
        result = score_expense(self._actual(category="Comida"), self._expected())
        assert result["expenses"][0]["fields"]["category"]["correct"] is True

    def test_merchant_accent_insensitive(self):
        result = score_expense(
            self._actual(merchant="Éxito"),
            {"count": 1, "expenses": [{"merchant": "exito"}]},
        )
        assert result["expenses"][0]["fields"]["merchant"]["correct"] is True

    def test_unspecified_field_is_skipped(self):
        # merchant is not in expected → not scored, does not affect all_correct
        result = score_expense(
            self._actual(merchant="RandomPlace"),
            {"count": 1, "expenses": [{"amount": 32000, "category": "comida"}]},
        )
        assert result["all_correct"] is True
        assert "merchant" not in result["expenses"][0]["fields"]

    def test_null_payment_method_correct(self):
        result = score_expense(self._actual(payment_method=None), self._expected())
        assert result["expenses"][0]["fields"]["payment_method"]["correct"] is True

    def test_float_amount_comparison(self):
        # Model may return 32000.0 instead of 32000
        result = score_expense(self._actual(amount=32000.0), self._expected())
        assert result["expenses"][0]["fields"]["amount"]["correct"] is True

    def test_usd_currency(self):
        actual = [{"amount": 20, "currency": "USD", "category": "servicios"}]
        expected = {"count": 1, "expenses": [{"amount": 20, "currency": "USD"}]}
        result = score_expense(actual, expected)
        assert result["expenses"][0]["fields"]["currency"]["correct"] is True


class TestScoreExpenseCount:
    def test_wrong_count_returns_false(self):
        actual = [{"amount": 32000, "currency": "COP", "category": "comida"}]
        expected = {"count": 2, "expenses": []}
        result = score_expense(actual, expected)
        assert result["all_correct"] is False
        assert result["count_correct"] is False
        assert result["expenses"] == []

    def test_too_many_expenses(self):
        actual = [
            {"amount": 32000, "currency": "COP", "category": "comida"},
            {"amount": 14000, "currency": "COP", "category": "transporte"},
        ]
        expected = {"count": 1, "expenses": [{"amount": 32000}]}
        result = score_expense(actual, expected)
        assert result["count_correct"] is False


class TestScoreExpenseMulti:
    def test_two_expenses_correct(self):
        actual = [
            {"amount": 20000, "currency": "COP", "category": "comida"},
            {"amount": 40000, "currency": "COP", "category": "entretenimiento"},
        ]
        expected = {
            "count": 2,
            "expenses": [
                {"amount": 20000, "category": "comida"},
                {"amount": 40000, "category": "entretenimiento"},
            ],
        }
        result = score_expense(actual, expected)
        assert result["all_correct"] is True
        assert result["count_correct"] is True
        assert len(result["expenses"]) == 2

    def test_second_expense_wrong(self):
        actual = [
            {"amount": 20000, "currency": "COP", "category": "comida"},
            {"amount": 99999, "currency": "COP", "category": "entretenimiento"},
        ]
        expected = {
            "count": 2,
            "expenses": [
                {"amount": 20000, "category": "comida"},
                {"amount": 40000, "category": "entretenimiento"},
            ],
        }
        result = score_expense(actual, expected)
        assert result["all_correct"] is False
        assert result["expenses"][0]["all_correct"] is True
        assert result["expenses"][1]["all_correct"] is False


# ── score_date_range ───────────────────────────────────────────────────────────

class TestScoreDateRange:
    def test_both_correct(self):
        result = score_date_range(
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
        )
        assert result["all_correct"] is True
        assert result["min_date"]["correct"] is True
        assert result["max_date"]["correct"] is True

    def test_wrong_min(self):
        result = score_date_range(
            {"min_date": "2026-06-02", "max_date": "2026-06-25"},
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
        )
        assert result["all_correct"] is False
        assert result["min_date"]["correct"] is False
        assert result["max_date"]["correct"] is True

    def test_wrong_max(self):
        result = score_date_range(
            {"min_date": "2026-06-01", "max_date": "2026-06-24"},
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
        )
        assert result["all_correct"] is False
        assert result["max_date"]["correct"] is False

    def test_both_wrong(self):
        result = score_date_range(
            {"min_date": "2026-05-01", "max_date": "2026-05-31"},
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
        )
        assert result["all_correct"] is False
        assert result["min_date"]["correct"] is False
        assert result["max_date"]["correct"] is False

    def test_actuals_preserved_in_result(self):
        result = score_date_range(
            {"min_date": "2026-06-02", "max_date": "2026-06-25"},
            {"min_date": "2026-06-01", "max_date": "2026-06-25"},
        )
        assert result["min_date"]["actual"] == "2026-06-02"
        assert result["min_date"]["expected"] == "2026-06-01"
