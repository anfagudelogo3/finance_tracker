"""Eval runner — calls the real OpenAI API and scores the results.

Usage:
    uv run python -m evals.run                  # print metrics table
    uv run python -m evals.run --check          # exit 1 if below thresholds
    PHOENIX_TRACING=1 uv run python -m evals.run  # same, traces sent to Phoenix

Prerequisites:
    uv sync --group evals
    # OPENAI_API_KEY must be set in .env or the environment
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# evals/__init__.py bootstraps sys.path and dummy env vars before src imports.
from finance_tracker.observability.tracing import setup_tracing
from finance_tracker.legacy.openai_parser import parse_expense, parse_report_request
from finance_tracker.agents.expenses import _extract as _extract_expense_claude
from finance_tracker.agents.incomes import _extract as _extract_income_claude
from finance_tracker.agents.reporting import resolve_date_range
from finance_tracker.routing.fast_paths import (
    fast_route,
    is_income_request,
    is_report_request,
)
from finance_tracker.routing import router_agent
from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.config import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_INCOME_CATEGORIES
from evals.scoring import score_expense, score_date_range, score_routing

DATASETS_DIR = Path(__file__).parent / "datasets"

# Minimum accuracy required per metric when --check is passed.
#
# "expense_agent" (Claude) thresholds are set with headroom below the observed
# claude-haiku-4-5-20251001 run against parse_expense.json (n=15): 100% overall,
# 100% amount, 100% category. Not pinned to 100% — that's a small sample, and a
# single new eval case or minor prompt change shouldn't break --check on one miss.
# Re-tighten as the dataset grows and the number stays stable.
#
# "income_agent" thresholds are set with headroom below the observed
# claude-haiku-4-5-20251001 run against parse_income.json (n=12): 100% overall,
# 100% amount, 100% currency, 100% category. Same reasoning as expense_agent — not
# pinned to 100%, small sample.
#
# "reporting_agent" thresholds are set with headroom below the observed run against
# parse_report_request.json (n=13, including the Claude-fallback case): 100% overall,
# 100% min_date, 100% max_date — confirming both parse_report_request failures (missing
# "hoy" rule, unreliable weekday arithmetic for "la semana pasada") are fixed, not just
# not-regressed. Same small-sample reasoning as expense_agent/income_agent.
#
# "router_agent" / "hybrid_routing" (Phase 5) are set with headroom below two observed
# claude-haiku-5-5 (effort low) runs against route_message.json (n=55 scored): 100% on
# every metric both times. Not pinned to 100% — small sample, and a few cases overlap
# examples in the router prompt. no_false_decline stays strict: refusing a real finance
# message loses a record. fast_path_precision must stay at 100% — nothing catches a
# wrong fast path (tests/routing/test_fast_paths.py enforces the same offline).
THRESHOLDS: dict[str, dict[str, float]] = {
    "router_agent": {
        "overall": 0.90,
        "no_false_decline": 0.98,
        "profile:business": 0.80,
        "adversarial": 0.80,
    },
    "hybrid_routing": {
        "overall": 0.90,
        "no_false_decline": 0.98,
        "profile:business": 0.80,
        "adversarial": 0.80,
        "fast_path_precision": 1.0,
    },
    "parse_expense": {
        "overall": 0.85,
        "amount": 0.95,
        "category": 0.85,
    },
    "expense_agent": {
        "overall": 0.90,
        "amount": 0.95,
        "category": 0.90,
    },
    "income_agent": {
        "overall": 0.90,
        "amount": 0.95,
        "category": 0.90,
    },
    "reporting_agent": {
        "overall": 0.90,
        "min_date": 0.90,
        "max_date": 0.90,
    },
    "parse_report_request": {
        "overall": 0.90,
        "min_date": 0.90,
        "max_date": 0.90,
    },
}


def _load_dataset(name: str) -> list[dict]:
    return json.loads((DATASETS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _run_scored_dataset(dataset_name: str, extract_fn) -> tuple[list[dict], dict]:
    """Run a parse_expense-shaped dataset through any side-effect-free extraction
    function of shape (text: str) -> list[dict], and score the results with
    score_expense. Shared by every expense-shaped agent (OpenAI expense, Claude
    expense, Claude income, ...) so they're all scored identically.
    """
    cases = _load_dataset(dataset_name)
    scores = []
    for case in cases:
        actual = extract_fn(case["input"])
        score = score_expense(actual, case["expected"])
        score["id"] = case["id"]
        scores.append(score)

    n = len(scores)
    overall = sum(1 for s in scores if s["all_correct"]) / n

    # Flatten per-field accuracy across all scored expenses
    field_hits: dict[str, list[bool]] = {}
    for s in scores:
        for exp_score in s.get("expenses", []):
            for field, fdata in exp_score.get("fields", {}).items():
                field_hits.setdefault(field, []).append(fdata["correct"])

    metrics: dict[str, int | float] = {"n": n, "overall": overall}
    for field, hits in field_hits.items():
        metrics[field] = sum(hits) / len(hits)

    return scores, metrics


def _run_parse_expense() -> tuple[list[dict], dict]:
    return _run_scored_dataset("parse_expense", parse_expense)


def _run_expense_agent() -> tuple[list[dict], dict]:
    """Same dataset, same scoring, run through the new Claude-based expense agent's
    extraction step (not handle(), which also persists to Postgres — evals have no
    live database) — this is the parity comparison against _run_parse_expense."""
    return _run_scored_dataset(
        "parse_expense",
        lambda text: _extract_expense_claude(text, DEFAULT_EXPENSE_CATEGORIES),
    )


def _run_income_agent() -> tuple[list[dict], dict]:
    """income_agent's extraction step against parse_income.json. No OpenAI-path
    counterpart exists — income never had an old-pipeline equivalent — so this
    establishes a fresh baseline rather than a parity comparison."""
    return _run_scored_dataset(
        "parse_income",
        lambda text: _extract_income_claude(text, DEFAULT_INCOME_CATEGORIES),
    )


def _run_date_range_dataset(dataset_name: str, resolve_fn) -> tuple[list[dict], dict]:
    """Run a parse_report_request-shaped dataset through any function of shape
    (case: dict) -> dict, and score with score_date_range. Shared by the OpenAI path
    and the new deterministic+Claude-fallback reporting_agent. Takes the whole case
    dict (not just input/now) so a wrapper can pull extra fields like `conversation`
    for cases that depend on it — resolve_fn decides what it needs."""
    cases = _load_dataset(dataset_name)
    scores = []
    for case in cases:
        actual = resolve_fn(case)
        score = score_date_range(actual, case["expected"])
        score["id"] = case["id"]
        scores.append(score)

    n = len(scores)
    overall = sum(1 for s in scores if s["all_correct"]) / n
    min_acc = sum(1 for s in scores if s["min_date"]["correct"]) / n
    max_acc = sum(1 for s in scores if s["max_date"]["correct"]) / n

    metrics: dict[str, int | float] = {
        "n": n,
        "overall": overall,
        "min_date": min_acc,
        "max_date": max_acc,
    }
    return scores, metrics


def _run_parse_report_request() -> tuple[list[dict], dict]:
    # Old path takes no conversation param and never will (Excel still depends on it,
    # unchanged) — cases that need conversation to resolve are expected failures here,
    # not regressions; it structurally can't do this.
    return _run_date_range_dataset(
        "parse_report_request",
        lambda case: parse_report_request(case["input"], case["now"]),
    )


def _run_reporting_agent() -> tuple[list[dict], dict]:
    """reporting_agent's date-range resolution (deterministic rules + Claude fallback
    for anything they don't cover, including referential follow-ups resolved against
    conversation history) against the same dataset — the fix for parse_report_request's
    known failures (missing "hoy" rule, unreliable LLM weekday arithmetic for "la semana
    pasada") plus the new follow-up capability (Phase 4)."""
    return _run_date_range_dataset(
        "parse_report_request",
        lambda case: resolve_date_range(
            case["input"],
            datetime.fromisoformat(case["now"]),
            case.get("conversation", []),
        ),
    )


# Tags reported as their own accuracy rows in routing evals.
_ROUTING_TAG_METRICS = [
    "single",
    "multi-intent",
    "ambiguous",
    "off-topic",
    "smalltalk",
    "adversarial",
    "follow-up",
    "business",
    "users",
]

# Every route_message.json case carries a profile; the same text can route differently
# for a personal user and a business (e.g. "almuerzo 20 mil" at a restaurant).
_PROFILE_TYPES = ["personal", "business"]


def _run_routing_dataset(route_fn) -> tuple[list[dict], dict]:
    """Run route_message.json through any function of shape (case: dict) -> dict
    returning {"route", "agents", optional "via"}, and score with score_routing.

    Cases carrying a "skip" key (capability not built yet, e.g. Phase 7 corrections)
    are left out. When route_fn reports "via": "fast_path", fast-path precision and
    coverage are added — a fast path has no LLM behind it to catch its mistakes, so
    its precision is the number that matters most.
    """
    cases = [c for c in _load_dataset("route_message") if "skip" not in c]
    scores = []
    for case in cases:
        actual = route_fn(case)
        score = score_routing(actual, case["expected"])
        score["id"] = case["id"]
        score["tags"] = case.get("tags", [])
        score["profile"] = case["profile"]["type"]
        score["via"] = actual.get("via")
        scores.append(score)

    n = len(scores)
    metrics: dict[str, int | float] = {
        "n": n,
        "overall": sum(1 for s in scores if s["all_correct"]) / n,
    }

    # A real finance message refused by the router is a lost record.
    should_delegate = [s for s in scores if s["route"]["expected"] == "agents"]
    if should_delegate:
        metrics["no_false_decline"] = sum(
            1 for s in should_delegate if s["route"]["actual"] != "decline"
        ) / len(should_delegate)

    for profile_type in _PROFILE_TYPES:
        profiled = [s for s in scores if s["profile"] == profile_type]
        if profiled:
            metrics[f"profile:{profile_type}"] = sum(
                1 for s in profiled if s["all_correct"]
            ) / len(profiled)

    for tag in _ROUTING_TAG_METRICS:
        tagged = [s for s in scores if tag in s["tags"]]
        if tagged:
            metrics[tag] = sum(1 for s in tagged if s["all_correct"]) / len(tagged)

    fast = [s for s in scores if s["via"] == "fast_path"]
    if fast:
        metrics["fast_path_precision"] = sum(1 for s in fast if s["all_correct"]) / len(
            fast
        )
        metrics["fast_path_coverage"] = len(fast) / n

    return scores, metrics


def _keyword_route(case: dict) -> dict:
    """Today's (Phase 4) routing, without dispatching: mirrors the decision order in
    orchestrator.handle_message — report keywords, then income keywords, then the
    expense default. It can only ever pick exactly one agent, never clarifies or
    declines, and ignores case["profile"] (today's code has no profile), so this is the
    "before" baseline for the Phase 5 router. Excel is left out: handler.py checks it
    before the orchestrator is reached."""
    text = case["input"]
    if is_report_request(text):
        return {"route": "agents", "agents": ["reporting"], "via": "fast_path"}
    if is_income_request(text):
        return {"route": "agents", "agents": ["incomes"], "via": "fast_path"}
    return {"route": "agents", "agents": ["expenses"], "via": "default"}


def _run_keyword_routing_baseline() -> tuple[list[dict], dict]:
    return _run_routing_dataset(_keyword_route)


# Stand-ins for the specialists during routing evals: the router's decision is what's
# scored, so no DB writes and no extra extraction calls.
_STUB_HANDLERS = {
    agent: (lambda request: AgentResponse(ok=True, reply_text="✅ registrado"))
    for agent in ("expenses", "incomes", "reporting")
}


def _router_route(case: dict) -> dict:
    """Live Claude router call (Phase 5) on one case, specialists stubbed."""
    request = AgentRequest(
        user_id=0,
        message_id=0,
        phone="+570000000000",
        text=case["input"],
        media=[],
        message_type="text",
        now="2026-06-25T10:00:00-05:00",
        conversation=case.get("conversation", []),
        profile=case["profile"],
    )
    try:
        result = router_agent.route(request, _STUB_HANDLERS)
    except Exception as exc:  # an API error is a failed case, not a crashed run
        return {"route": f"error:{type(exc).__name__}", "agents": [], "via": "router"}
    return {"route": result.route, "agents": result.agents, "via": "router"}


def _hybrid_route(case: dict) -> dict:
    """What production does (orchestrator.handle_message): fast path, else router."""
    agent = fast_route(case["input"], case["profile"], case.get("conversation"))
    if agent:
        return {"route": "agents", "agents": [agent], "via": "fast_path"}
    return _router_route(case)


def _run_router_agent() -> tuple[list[dict], dict]:
    return _run_routing_dataset(_router_route)


def _run_hybrid_routing() -> tuple[list[dict], dict]:
    return _run_routing_dataset(_hybrid_route)


_WIDTH = 54


def _print_section(title: str, scores: list[dict], metrics: dict) -> None:
    print(f"\n{'─' * _WIDTH}")
    print(f"  {title}  (n={metrics['n']})")
    print(f"{'─' * _WIDTH}")

    for key, val in metrics.items():
        if key == "n":
            continue
        pct = float(val)
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
        print(f"  {key:<18} {bar}  {pct:.0%}")

    failures = [s for s in scores if not s["all_correct"]]
    if not failures:
        return

    print(f"\n  Failures ({len(failures)}):")
    for s in failures:
        print(f"    [{s['id']}]")
        if "expenses" in s and not s.get("count_correct", True):
            print("      count: wrong number of expenses extracted")
        for exp_score in s.get("expenses", []):
            for field, fdata in exp_score.get("fields", {}).items():
                if not fdata["correct"]:
                    print(f"      {field}: expected={fdata['expected']!r}  got={fdata['actual']!r}")
        for field in ("min_date", "max_date", "route", "agents"):
            if field in s and not s[field]["correct"]:
                print(f"      {field}: expected={s[field]['expected']!r}  got={s[field]['actual']!r}")


def _check_thresholds(name: str, metrics: dict) -> list[str]:
    failed = []
    for key, threshold in THRESHOLDS.get(name, {}).items():
        actual_val = float(metrics.get(key, 0.0))
        if actual_val < threshold:
            failed.append(f"  {name}/{key}: {actual_val:.0%} < {threshold:.0%} required")
    return failed


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Run LLM evals against the live OpenAI API"
    )
    arg_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with code 1 if any metric falls below its threshold",
    )
    arg_parser.add_argument(
        "--only",
        choices=["routing"],
        help="Run a single eval group. 'routing' needs only ANTHROPIC_API_KEY.",
    )
    args = arg_parser.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    has_anthropic_key = bool(anthropic_key) and anthropic_key != "sk-ant-not-set"

    if args.only == "routing":
        if setup_tracing():
            print("Phoenix tracing enabled.")
        _finish(_run_routing_sections(args.check, has_anthropic_key))
        return

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "sk-not-set":
        sys.exit("OPENAI_API_KEY not configured. Add it to your .env file.")

    if setup_tracing():
        print("Phoenix tracing enabled.")

    print("Running evals against the live OpenAI API...")

    threshold_failures: list[str] = []

    scores_exp, metrics_exp = _run_parse_expense()
    _print_section("parse_expense", scores_exp, metrics_exp)
    if args.check:
        threshold_failures.extend(_check_thresholds("parse_expense", metrics_exp))

    scores_rep, metrics_rep = _run_parse_report_request()
    _print_section("parse_report_request", scores_rep, metrics_rep)
    if args.check:
        threshold_failures.extend(_check_thresholds("parse_report_request", metrics_rep))

    threshold_failures.extend(_run_routing_sections(args.check, has_anthropic_key))

    if has_anthropic_key:
        print("\nRunning expense_agent (Claude) against the same dataset for parity...")
        scores_agent, metrics_agent = _run_expense_agent()
        _print_section("expense_agent (Claude)", scores_agent, metrics_agent)
        if args.check:
            threshold_failures.extend(_check_thresholds("expense_agent", metrics_agent))

        print("\nRunning income_agent (Claude) against parse_income.json...")
        scores_income, metrics_income = _run_income_agent()
        _print_section("income_agent (Claude)", scores_income, metrics_income)
        if args.check:
            threshold_failures.extend(_check_thresholds("income_agent", metrics_income))

        print(
            "\nRunning reporting_agent (deterministic + Claude fallback) for parity..."
        )
        scores_report_agent, metrics_report_agent = _run_reporting_agent()
        _print_section("reporting_agent", scores_report_agent, metrics_report_agent)
        if args.check:
            threshold_failures.extend(
                _check_thresholds("reporting_agent", metrics_report_agent)
            )
    else:
        print(
            "\nSkipping expense_agent/income_agent/reporting_agent (Claude) evals — "
            "ANTHROPIC_API_KEY not configured. Add it to your .env file to run them."
        )

    _finish(threshold_failures)


def _run_routing_sections(check: bool, has_anthropic_key: bool) -> list[str]:
    """Keyword baseline (free), then the live router alone and the hybrid path
    (fast paths first, router for the rest — what production runs)."""
    failures: list[str] = []

    # Deterministic, no API calls. Informational only — no thresholds: it's the
    # "before" number the Phase 5 router is measured against, not a gate.
    scores_route, metrics_route = _run_keyword_routing_baseline()
    _print_section("routing — keyword baseline (Phase 4)", scores_route, metrics_route)

    if not has_anthropic_key:
        print("\nSkipping router / hybrid routing evals — ANTHROPIC_API_KEY not configured.")
        return failures

    print("\nRunning router_agent (Claude) on every case, specialists stubbed...")
    scores_router, metrics_router = _run_router_agent()
    _print_section("routing — router_agent alone", scores_router, metrics_router)
    if check:
        failures.extend(_check_thresholds("router_agent", metrics_router))

    print("\nRunning hybrid routing (fast paths, then router) — the production path...")
    scores_hybrid, metrics_hybrid = _run_hybrid_routing()
    _print_section("routing — hybrid (production)", scores_hybrid, metrics_hybrid)
    if check:
        failures.extend(_check_thresholds("hybrid_routing", metrics_hybrid))
    return failures


def _finish(threshold_failures: list[str]) -> None:
    if threshold_failures:
        print(f"\n{'─' * _WIDTH}")
        print("Threshold failures:")
        for msg in threshold_failures:
            print(f"  ✗ {msg}")
        sys.exit(1)

    print(f"\n{'─' * _WIDTH}")
    print("Done.")


if __name__ == "__main__":
    main()
