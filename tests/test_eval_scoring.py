"""Offline unit tests for evals/scoring.py.

No LLM calls, no network — runs in the normal test suite (uv run pytest tests/).
These tests verify the scoring logic itself so the eval harness is trustworthy.
"""
import json
from pathlib import Path

import pytest

from evals.scoring import score_expense, score_date_range, score_routing, score_users


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
                business = profile["business"]
                assert business["industry"], case["id"]
                # Onboarding stores sells/main_costs; older cases use description
                assert business.get("sells") or business.get("description"), case["id"]
            else:
                assert "business" not in profile, case["id"]

    def test_conversation_turns_well_formed(self, cases):
        for case in cases:
            for turn in case.get("conversation", []):
                assert turn["role"] in {"user", "assistant"}, case["id"]
                assert turn["content"], case["id"]


class TestScoreUsers:
    def test_exact_match(self):
        actual = {"action": "rename_category", "confirmation": True,
                  "args": {"kind": "expense", "name": "comida", "new_name": "alimentación"}}
        expected = {"action": "rename_category", "confirmation": True,
                    "args": {"name": "comida", "new_name": "alimentacion"}}
        assert score_users(actual, expected)["all_correct"]

    def test_wrong_action(self):
        result = score_users({"action": "none"}, {"action": "add_category", "args": {"name": "x"}})
        assert not result["all_correct"]
        assert not result["action"]["correct"] and not result["args"]["correct"]

    def test_string_args_match_by_containment(self):
        actual = {"action": "update_business_details", "args": {"sells": "Almuerzos y domicilios"}}
        assert score_users(actual, {"action": "update_business_details",
                                    "args": {"sells": "domicilios"}})["all_correct"]

    def test_missing_arg_is_wrong(self):
        result = score_users({"action": "add_category", "args": {}},
                             {"action": "add_category", "args": {"name": "mascotas"}})
        assert not result["args"]["correct"]

    def test_confirmation_checked_only_when_expected(self):
        actual = {"action": "deactivate_category", "args": {"name": "ropa"}, "confirmation": True}
        assert score_users(actual, {"action": "deactivate_category"})["all_correct"]
        assert not score_users(actual, {"action": "deactivate_category",
                                        "confirmation": False})["all_correct"]

    def test_none_with_no_args(self):
        assert score_users({"action": "none", "args": {}}, {"action": "none"})["all_correct"]


class TestUsersAgentDataset:
    @pytest.fixture(scope="class")
    def cases(self):
        path = Path(__file__).parents[1] / "evals" / "datasets" / "users_agent.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_ids_unique_and_actions_known(self, cases):
        from finance_tracker.agents.users import TOOLS

        tools = {t["name"] for t in TOOLS} - {"get_settings"}
        assert len({c["id"] for c in cases}) == len(cases)
        for case in cases:
            assert case["expected"]["action"] in tools | {"none"}, case["id"]
            assert case["profile"]["type"] in {"personal", "business"}, case["id"]


class TestScoreExpenseDate:
    def test_date_is_scored_when_expected(self):
        actual = [{"amount": 10000, "date": "2026-10-21"}]
        assert score_expense(actual, {"count": 1, "expenses": [{"amount": 10000, "date": "2026-10-21"}]})["all_correct"]
        assert not score_expense(actual, {"count": 1, "expenses": [{"date": "2026-10-22"}]})["all_correct"]
