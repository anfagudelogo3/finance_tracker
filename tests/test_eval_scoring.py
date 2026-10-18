"""Offline unit tests for evals/scoring.py.

No LLM calls, no network — runs in the normal test suite (uv run pytest tests/).
These tests verify the scoring logic itself so the eval harness is trustworthy.
"""
import json
from pathlib import Path

import pytest

from evals.scoring import score_expense, score_date_range, score_routing


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


# ── score_routing ──────────────────────────────────────────────────────────────

class TestScoreRouting:
    def test_single_agent_correct(self):
        result = score_routing(
            {"route": "agents", "agents": ["expenses"]},
            {"route": "agents", "agents": ["expenses"]},
        )
        assert result["all_correct"] is True

    def test_agents_order_insensitive(self):
        result = score_routing(
            {"route": "agents", "agents": ["incomes", "expenses"]},
            {"route": "agents", "agents": ["expenses", "incomes"]},
        )
        assert result["all_correct"] is True

    def test_missing_agent(self):
        result = score_routing(
            {"route": "agents", "agents": ["expenses"]},
            {"route": "agents", "agents": ["expenses", "incomes"]},
        )
        assert result["all_correct"] is False
        assert result["route"]["correct"] is True
        assert result["agents"]["correct"] is False

    def test_extra_agent(self):
        result = score_routing(
            {"route": "agents", "agents": ["expenses", "reporting"]},
            {"route": "agents", "agents": ["expenses"]},
        )
        assert result["agents"]["correct"] is False

    def test_wrong_route_type(self):
        result = score_routing(
            {"route": "agents", "agents": ["expenses"]},
            {"route": "clarify", "agents": []},
        )
        assert result["all_correct"] is False
        assert result["route"]["correct"] is False

    def test_clarify_correct(self):
        result = score_routing({"route": "clarify"}, {"route": "clarify", "agents": []})
        assert result["all_correct"] is True

    def test_decline_with_delegation_is_wrong(self):
        result = score_routing(
            {"route": "decline", "agents": ["reporting"]},
            {"route": "decline", "agents": []},
        )
        assert result["all_correct"] is False
        assert result["agents"]["correct"] is False

    def test_none_agents_treated_as_empty(self):
        result = score_routing(
            {"route": "none", "agents": None}, {"route": "none", "agents": []}
        )
        assert result["all_correct"] is True


# ── route_message.json integrity ───────────────────────────────────────────────

_ROUTE_DATASET = (
    Path(__file__).parent.parent / "evals" / "datasets" / "route_message.json"
)
_VALID_ROUTES = {"agents", "clarify", "decline", "none"}
_VALID_AGENTS = {"expenses", "incomes", "reporting", "users"}


class TestRouteMessageDataset:
    @pytest.fixture(scope="class")
    def cases(self):
        return json.loads(_ROUTE_DATASET.read_text(encoding="utf-8"))

    def test_ids_unique(self, cases):
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids))

    def test_expected_shape(self, cases):
        for case in cases:
            expected = case["expected"]
            assert expected["route"] in _VALID_ROUTES, case["id"]
            assert set(expected["agents"]) <= _VALID_AGENTS, case["id"]
            # Delegation only on the "agents" route, and that route always delegates
            delegates = expected["route"] == "agents"
            assert bool(expected["agents"]) == delegates, case["id"]

    def test_profile_shape(self, cases):
        for case in cases:
            profile = case["profile"]
            assert profile["type"] in {"personal", "business"}, case["id"]
            if profile["type"] == "business":
                # Classification needs to know what the business sells and pays for
                assert profile["business"]["industry"], case["id"]
                assert profile["business"]["description"], case["id"]
            else:
                assert "business" not in profile, case["id"]

    def test_conversation_turns_well_formed(self, cases):
        for case in cases:
            for turn in case.get("conversation", []):
                assert turn["role"] in {"user", "assistant"}, case["id"]
                assert turn["content"], case["id"]
