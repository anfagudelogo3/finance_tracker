# Phase 5 — Router agent (agents-as-tools), profile-aware — Report

Plan: [agent-roadmap.md](../../agent-roadmap.md) §4 and §7 (Phase 5).

## What was built

- `core/agent_loop.py` — generic Claude tool-use loop. Continues while `stop_reason ==
  "tool_use"`, ends normally only on `end_turn`. `max_iterations` (6) and a wall-clock budget
  (12 s) are safeguards; `max_tokens` never executes a possibly truncated tool call;
  `refusal` and unexpected stop reasons are handled; a failing tool becomes an `is_error`
  result and the loop continues.
- `routing/router_agent.py` — Claude router with five tools: `delegate_to_expenses`,
  `delegate_to_incomes`, `delegate_to_reporting` (each runs the existing agent's
  `handle(AgentRequest)`), `ask_clarification`, `decline_out_of_scope`. Multi-intent messages
  produce several delegations (max 3). The reply is assembled in code from the agents'
  confirmations; the router's own text is used only for small talk.
- `routing/fast_paths.fast_route` — the narrowed free path in front of the router, used by
  `handler.py`'s report branch and by `orchestrator.handle_message`.
- `routing/orchestrator.py` — hybrid dispatch: `fast_route`, else the router.
- User profile: `AgentRequest.profile`, loaded in `handler.py` from the new append-only
  `user_prefs` table (`database.get_user_prefs` reads the latest row;
  `database.append_user_prefs` is the only writer, ready for Phase 6). `users.prefs` is
  deprecated.
- `config.py`: router model and limits, `MSG_OUT_OF_SCOPE`, `MSG_ROUTER_EMPTY`,
  `MSG_DELEGATION_FAILED`, `MSG_PARTIAL_TIMEOUT`, `DEFAULT_PROFILE`.
- Evals: router-alone and hybrid sections in `evals/run.py`, `--only routing`, thresholds.
- Tests: 115 → 219. New `tests/core/test_agent_loop.py`, `tests/routing/test_router_agent.py`,
  `tests/routing/test_fast_paths.py` (fast path never wrong on the dataset); `tests/conftest.py`
  fails any test that reaches a live Claude/OpenAI client.

## Decisions made / changed

- **Router model `claude-haiku-5-5` at `effort: "low"`**, not the Haiku 4.5 the specialists use:
  it's the current Haiku tier, cheaper per token ($0.10 / $0.50 vs $1 / $5 per MTok), and good
  at tool selection with `tool_choice: auto`. It thinks by default, so the loop appends
  assistant content unmodified and `ROUTER_MAX_TOKENS` (2048) leaves room for thinking.
- **Reply assembled in code** instead of composed by the router — guarantees the router can't
  invent or restate amounts (the "output" guardrail).
- **Fast paths narrowed** after the baseline showed 31% precision: no fuzzy report matching,
  single report words only in ≤3-word messages, personal profiles only, never after a bot
  question, never with conjunctions / `", "` / `;` / destructive or injection words / phone
  numbers, and a new simple-expense path (amount + clear expense word, ≤7 words).
- **Preferences are append-only** (user request during the phase): every change is a new
  `user_prefs` row with full snapshot, changed keys, source and datetime; reads take the latest
  row. Replaces reading `users.prefs`.
- Conversation + message sent as tagged data in one user turn (injection hardening, and no
  role-order issue when history starts with an assistant turn).

## Eval results

`uv run python -m evals.run --only routing`, `route_message.json`, 55 scored cases (45 personal,
10 business; 9 more are skipped until Phases 6 and 8):

| Metric | Phase 4 keywords | Router alone | Hybrid (production) |
|---|---|---|---|
| Overall | 27% | **100%** | **100%** |
| Personal / business profile | 24% / 40% | 100% / 100% | 100% / 100% |
| Multi-intent, ambiguous, off-topic, small talk, adversarial, follow-up | 0–25% | 100% | 100% |
| Fast-path precision / coverage | 31% / 24% | — | 100% / 16% |

Two router runs, both 55/55. Thresholds set with headroom (`overall` 90%, `no_false_decline`
98%, `fast_path_precision` 100%) — see the comment above `THRESHOLDS`.

**Caveat:** several cases overlap examples written into the router prompt ("almuerzo 20k y me
pagaron 2M", "nequi 50000", "fue un gasto, mercado"), and n is small. The numbers show the
design works; they don't yet prove generalization. Add unseen phrasings (ideally real messages)
before tightening thresholds.

The specialist evals (`expense_agent`, `income_agent`, `reporting_agent`) were not re-run: no
specialist code or prompt changed.

## Phoenix review (bottlenecks)

**Not done yet** — needs a run with `PHOENIX_TRACING=1` and a few real messages. Measured
without Phoenix (router alone, specialists stubbed, second eval run):

- Router latency: p50 2.46 s, p90 3.69 s, max 6.92 s. Add one specialist call (~1–2 s) for
  messages that delegate.
- Coverage: only 16% of dataset messages take the free fast path, but the dataset is weighted
  toward hard cases; real traffic is mostly simple expenses, which the fast path covers.
- Tokens / cost per message: not measured yet — read `usage` from the `Agent loop iteration`
  log lines or from Phoenix.

## Follow-ups / tech debt

- Phoenix trace review (latency per span, tokens, cost per message) before calling the phase
  closed.
- Grow `route_message.json` with phrasings that don't appear in the prompt, and real messages.
- **Deploy order:** run `scripts/setup_db.sql` on Neon (creates `user_prefs`) *before* deploying
  this code — the handler reads that table on every message.
- Routing latency on WhatsApp: p90 ≈ 4–5 s end to end for router messages. Watch it; options if
  it hurts: widen the fast paths with eval evidence, or prompt caching once the prompt is above
  the cacheable minimum.
- Excel still resolves dates with `legacy.openai_parser.parse_report_request` (OpenAI), not
  the reporting agent's deterministic resolver.
