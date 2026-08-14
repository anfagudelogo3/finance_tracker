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
from pathlib import Path

# evals/__init__.py bootstraps sys.path and dummy env vars before src imports.
from tracing import setup_tracing
from parser import parse_expense, parse_report_request
from expense_agent import _extract as _extract_expense_claude
from config import DEFAULT_EXPENSE_CATEGORIES
from evals.scoring import score_expense, score_date_range

DATASETS_DIR = Path(__file__).parent / "datasets"

# Minimum accuracy required per metric when --check is passed.
#
# "expense_agent" (Claude) thresholds are set with headroom below the observed
# claude-haiku-4-5-20251001 run against parse_expense.json (n=15): 100% overall,
# 100% amount, 100% category. Not pinned to 100% — that's a small sample, and a
# single new eval case or minor prompt change shouldn't break --check on one miss.
# Re-tighten as the dataset grows and the number stays stable.
THRESHOLDS: dict[str, dict[str, float]] = {
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
    "parse_report_request": {
        "overall": 0.90,
        "min_date": 0.90,
        "max_date": 0.90,
    },
}


def _load_dataset(name: str) -> list[dict]:
    return json.loads((DATASETS_DIR / f"{name}.json").read_text())


def _run_expense_dataset(extract_fn) -> tuple[list[dict], dict]:
    """Run the parse_expense dataset through any side-effect-free extraction
    function of shape (text: str) -> list[dict], and score the results.
    Shared by the OpenAI and Claude expense paths so they're scored identically.
    """
    cases = _load_dataset("parse_expense")
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
    return _run_expense_dataset(parse_expense)


def _run_expense_agent() -> tuple[list[dict], dict]:
    """Same dataset, same scoring, run through the new Claude-based expense agent's
    extraction step (not handle(), which also persists to Postgres — evals have no
    live database) — this is the parity comparison against _run_parse_expense."""
    return _run_expense_dataset(
        lambda text: _extract_expense_claude(text, DEFAULT_EXPENSE_CATEGORIES)
    )


def _run_parse_report_request() -> tuple[list[dict], dict]:
    cases = _load_dataset("parse_report_request")
    scores = []
    for case in cases:
        actual = parse_report_request(case["input"], case["now"])
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
        for field in ("min_date", "max_date"):
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
    args = arg_parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "sk-not-set":
        sys.exit("OPENAI_API_KEY not configured. Add it to your .env file.")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    has_anthropic_key = bool(anthropic_key) and anthropic_key != "sk-ant-not-set"

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

    if has_anthropic_key:
        print("\nRunning expense_agent (Claude) against the same dataset for parity...")
        scores_agent, metrics_agent = _run_expense_agent()
        _print_section("expense_agent (Claude)", scores_agent, metrics_agent)
        if args.check:
            threshold_failures.extend(_check_thresholds("expense_agent", metrics_agent))
    else:
        print(
            "\nSkipping expense_agent (Claude) eval — ANTHROPIC_API_KEY not configured. "
            "Add it to your .env file to compare the new agent against parse_expense."
        )

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
