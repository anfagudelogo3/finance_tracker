# Agent Roadmap — Router + Specialist Agents (Phases 5+)

**Status:** planning document. Phases 0–4 are shipped and recorded in
[agent-architecture.md](agent-architecture.md), which stays the source of truth for *what
exists*. This document plans *what comes next*: turning keyword dispatch into a
router-driven, conversational multi-agent system.

---

## 1. Context & goals

### Where we are (Phases 0–4, shipped)

| Piece | Where |
|---|---|
| Identity from the Twilio-verified phone | `database.get_or_create_user` |
| Per-user categories (data, not prompt constants) | `categories` table, `database.get_user_categories` |
| Short-term memory (8 turns / 30 min) | `conversation_turns`, `database.save_turn` / `load_recent_turns` |
| Agent contract | `src/agent_types.py` — `AgentRequest` / `AgentResponse`, `handle(request) -> response` |
| Dispatch | `src/orchestrator.py` — deterministic keywords: report → income → expense default |
| Specialists | `src/expense_agent.py`, `src/income_agent.py`, `src/reporting_agent.py` (Claude Haiku, forced tool-use) |

### What's missing

- **Routing is keyword-only.** There's no LLM router, so off-topic text, multi-intent messages
  ("almuerzo 20k y me pagaron 2M") and ambiguous phrasing all fall through to the expense agent.
- **No guardrails** beyond the code-level ones (signature check, allowlist, `user_id` scoping).
- **No conversational corrections** ("no, eran 15000", "borra el último").
- **WhatsApp is the only input channel.**

### Goal

A **router (main) agent with guardrails** that sends each task to one or more specialists
(**expenses, incomes, reporting**), uses per-user conversation context, and composes a single
reply. The design should make it possible to add non-WhatsApp sources (email) later without
changing the agents.

> **Scope decision:** a *users* agent (profile, category/budget management) was considered and
> **dropped for now**. The agents-as-tools contract below lets it be added later as one more
> `delegate_to_*` tool (see §7).

---

## 2. Framework decision: agents-as-tools on the `anthropic` SDK

We considered the Claude Agent SDK. The routing pattern we need (a router handing tasks to
subagents and composing their results) is **"agents-as-tools"**, and it's a short tool-use loop on
the `anthropic` SDK the project already uses.

| Concern | Agents-as-tools (`anthropic` SDK) | Claude Agent SDK |
|---|---|---|
| What it is | Your own loop over tools you define | Claude Code as a library: CLI subprocess, built-in file/bash/web tools, subagents, hooks, sessions |
| Fit for our specialists | Exact: specialists are structured extractors writing to Postgres | Most built-ins (file edit, bash, grep) are unused and must be disabled |
| Deployment | Unchanged: Lambda zip, `deploy.sh`, flat imports | Container image (or long-running server), writable HOME, bundled CLI |
| Memory | Reuses `conversation_turns` | Has its own session store (local files), which would duplicate Postgres |
| Cost / latency per message | One small Haiku router call + the specialist call | Larger default prompt, multi-turn harness on every message |
| Guardrail control | Every tool executor is our Python; `user_id` injected by us | Possible via hooks, but through more layers |

### Confirmed decisions

| Decision | Choice |
|---|---|
| Hosting | **AWS Lambda** (zip deploy via `scripts/deploy.sh`, unchanged) |
| Routing order | **Hybrid:** deterministic keyword fast paths run first; the LLM router handles only what they don't resolve |
| Framework | Agents-as-tools on the `anthropic` SDK (no Agent SDK in the request path) |
| Specialists | Expenses, incomes, reporting (users agent deferred) |

**Decision:** build the router and its loop on the `anthropic` SDK. Revisit the Agent SDK for
**Phase 9** (email ingestion / deeper analysis worker). That work is background, not
latency-sensitive, and benefits from a fuller harness.

---

## 3. Target architecture

```
WhatsApp ─► Twilio ─► handler.py  (UNCHANGED: signature verify → allowlist → get_or_create_user
                         │          → save_message (idempotent) → load_recent_turns → save_turn
                         │          → store_all_media → transcribe audio)
                         ▼
               orchestrator.handle_message(AgentRequest)
                         │
          ┌──────────────┴───────────────┐
          │ keyword fast paths (free)    │  excel / report / income / simple-expense heuristic
          └──────────────┬───────────────┘
                         │ no confident match
                         ▼
               router_agent.route(request)          ◄── guardrails (§5)
                 tool-use loop (agent_loop.py, §4.3)
          ┌──────────┬───────────┬──────────────┬──────────────────────┐
          ▼          ▼           ▼              ▼                      ▼
  delegate_to_  delegate_to_  delegate_to_  decline_out_of_scope   ask_clarification
   expenses      incomes       reporting
      │             │              │
 expense_agent  income_agent  reporting_agent   (existing handle(AgentRequest))
      └─────────────┴──────────────┘
                         │ tool_results
                         ▼
               router composes ONE reply ─► handler.py sends via Twilio, save_turn
```

**Routing order is fixed: fast paths first, then the router.** The keyword fast paths stay
because they're free and already eval-covered. The router only pays for itself on messages the
keywords can't resolve confidently. Everything runs inside the existing Lambda.

---

## 4. Router ↔ subagent contract

### 4.1 Tools the router sees

| Tool | Input | Executor |
|---|---|---|
| `delegate_to_expenses` | `{task: str}` | `expense_agent.handle(child_request)` |
| `delegate_to_incomes` | `{task: str}` | `income_agent.handle(child_request)` |
| `delegate_to_reporting` | `{task: str}` | `reporting_agent.handle(child_request)` |
| `decline_out_of_scope` | `{reason: str}` | returns `config.MSG_OUT_OF_SCOPE` |
| `ask_clarification` | `{question: str}` | returns the question as the reply (no write) |

`task` is the router's rewrite of the relevant part of the user's message. For example,
"almuerzo 20k y me pagaron 2M" becomes two delegations: `"almuerzo 20000"` to expenses and
`"me pagaron 2000000"` to incomes. Adding a specialist later means one new `delegate_to_*` tool
plus one executor entry.

### 4.2 Executor rules

- The executor builds a **child `AgentRequest`**: same `user_id`, `message_id`, `phone`, `now`,
  `conversation` and `message_type`, with `text=task`. The model never supplies `user_id`; no
  tool schema has a user field.
- On success it returns `reply_text` plus compact `data` (ids, amounts, categories) as the
  `tool_result`.
- If a specialist raises, the executor returns a `tool_result` with `is_error: true` and a short
  message. The router can then report partial success honestly ("registré el gasto, pero no pude
  registrar el ingreso"). `handler.py`'s top-level `try/except` → `MSG_ERROR` → `200` stays as the
  backstop.
- The router receives the bounded history from `load_recent_turns` so it can resolve follow-ups.
  Specialists keep receiving it through `AgentRequest.conversation`.

### 4.3 Agent loop semantics (`src/agent_loop.py::run_tool_loop`)

This is a shared helper. The router uses it from Phase 5, and any specialist that becomes
multi-step (Phase 7 corrections) will use it too. Today's specialists stay as a single
forced-tool call and don't need a loop.

**The primary stop condition is the model's `stop_reason`, not a counter.**

```python
messages = history + [{"role": "user", "content": request.text}]
for iteration in range(max_iterations):            # safeguard, not the stop mechanism
    if elapsed() > timeout_seconds:                # second safeguard
        return _safeguard_reply(executed, reason="timeout")

    response = client.messages.create(model=..., system=..., tools=TOOLS,
                                      tool_choice={"type": "auto"}, messages=messages)
    log_usage(iteration, response.stop_reason, response.usage)

    if response.stop_reason == "end_turn":         # normal, model-decided finish
        return _final_text(response)

    if response.stop_reason == "tool_use":         # model wants more work done → keep going
        messages.append({"role": "assistant", "content": response.content})
        results = [execute(block) for block in response.content if block.type == "tool_use"]
        messages.append({"role": "user", "content": results})   # ALL results in ONE message
        executed.extend(results)
        continue

    if response.stop_reason == "max_tokens":       # possibly truncated tool_use: never execute it
        return _safeguard_reply(executed, reason="max_tokens")
    if response.stop_reason == "refusal":
        return MSG_OUT_OF_SCOPE
    return _safeguard_reply(executed, reason=f"unexpected:{response.stop_reason}")

return _safeguard_reply(executed, reason="max_iterations")
```

- **The loop keeps going while `stop_reason == "tool_use"`** and ends normally only on
  **`end_turn`**. A multi-step task (delegate to expenses, then incomes, then compose the reply)
  is never cut off midway.
- **Other stop reasons are handled explicitly.** A `max_tokens` stop may contain a truncated
  `tool_use`, so it's never executed. A `refusal` gets a polite deflection. Any other reason is
  logged and gets a friendly reply. No exception escapes, so `handler.py`'s 200 contract holds.
- **`max_iterations` is a safeguard, not the stopping mechanism.** `config.ROUTER_MAX_ITERATIONS`
  starts at **6**, well above the expected 2–3 calls. Hitting it means something is wrong (tool
  ping-pong, the model not converging). The loop logs a warning with the trace and replies with
  what was already done. Specialist writes are already persisted and get listed from the
  collected `tool_result`s, so no work is silently dropped.
- **Wall-clock budget:** `config.AGENT_LOOP_TIMEOUT_SECONDS` is checked before each call, kept
  under Twilio's 15 s webhook timeout. Twilio retries are already safe because `save_message` is
  idempotent.
- **Observability:** stop reason and `response.usage` are logged per iteration, so logs and
  Phoenix show how many iterations real messages take. Tune `max_iterations` from that data, not
  by guesswork.
- **Alternative:** `client.beta.messages.tool_runner` would also work. The hand-written loop was
  chosen because the stop-reason handling and safeguards are explicit, beta-free and unit-testable.

### 4.4 Model and cost

- New `config.CLAUDE_ROUTER_MODEL`, Haiku tier. It uses `tool_choice: auto`; the specialists
  keep their forced tool choice.
- **Cost flag:** any message that misses the fast paths gains one Haiku router call (about
  +0.5–1 s). Expenses are most of the traffic, so Phase 5 also evaluates a "simple expense"
  heuristic fast path (an amount is present, the message is short, and there's no
  conjunction/income/report signal). Those messages skip the router.

---

## 5. Guardrails (defense in depth)

| Layer | Catches | Mechanism |
|---|---|---|
| **Code (the floor)** | Cross-user access, data leakage | `user_id` only from the verified phone, injected by the executor; no tool takes a user id; no raw SQL from the model; all writes scoped by `user_id`; soft-delete only |
| **Input** | Abuse, oversized or empty input | Length cap; media-only / empty handling; user text treated as data in the system prompt (injection hardening) |
| **Scope** | Off-topic ("escríbeme un poema") | `decline_out_of_scope` → `config.MSG_OUT_OF_SCOPE` (Spanish) |
| **Loop** | Runaway cost / latency | `stop_reason`-driven loop + `max_iterations` + wall-clock budget + per-call token logging (§4.3) |
| **Output** | Hallucinated numbers | The reply is composed from specialist `reply_text`s; the router may join or summarize but must not invent amounts. Checked in evals |

Unchanged and never weakened: `webhook.verify_signature`, the phone allowlist, and
`save_message` idempotency.

---

## 6. Phased rollout

Each phase follows the existing working agreements: its own branch and PR, tests and evals green,
docs updated, a phase report in `docs/design/phase-reports/`, and a Phoenix trace review covering
latency, tokens and cost per message.

### Phase 5 — Router agent (agents-as-tools)

- New: `src/agent_loop.py` (§4.3) and `src/router_agent.py` (tools + executors, §4.1–4.2).
- `orchestrator.handle_message`: keep the fast paths, add the simple-expense heuristic, and send
  everything else to `router_agent.route`.
- Multi-intent delegation, `decline_out_of_scope` and `ask_clarification`.
- `config.py`: `CLAUDE_ROUTER_MODEL`, `ROUTER_MAX_ITERATIONS`, `AGENT_LOOP_TIMEOUT_SECONDS`,
  `MSG_OUT_OF_SCOPE`.
- **Evals:** new `evals/datasets/route_message.json` (message → expected agent set, or decline /
  clarify), plus `score_routing` in `evals/scoring.py` (pure Python, unit-tested).
- **Tests:** `tests/test_agent_loop.py` with mocked Claude sequences covering:
  tool_use → tool_use → end_turn; reaching `max_iterations`; `max_tokens` with a partial
  tool_use; refusal; timeout; a specialist raising (→ `is_error` result, loop continues).
  Also `tests/test_router_agent.py` for executor wiring and the `user_id` injection invariant.
- **Exit:** routing eval above threshold; existing expense/income/report evals unchanged; Phoenix
  shows router → specialist spans.

### Phase 6 — Guardrails hardening

- Adversarial and off-topic eval cases (prompt injection, "ignore previous instructions",
  requests for another user's data, non-finance chat).
- Input length cap and empty-message handling; per-message token/cost logging summarized in the
  phase report.
- **Exit:** adversarial dataset passes; there's no path where model output reaches SQL or
  chooses `user_id`.

### Phase 7 — Conversational corrections

- Edit, delete and undo for expenses and incomes on the last or a referenced record ("no, eran
  15000", "borra el último", "deshaz eso"). These are new tools inside the expense and income
  agents, which become multi-step and use `run_tool_loop`. They are not a new agent.
- Soft delete via the existing `deleted_at` column. `get_expenses` and reporting must exclude
  soft-deleted rows.
- Reporting extended to incomes and net cashflow.
- **Evals:** a correction/deletion dataset where the score checks which record changed and its
  final values.

### Phase 8 — Channel abstraction

- Normalize inbound into a channel-agnostic message (`channel`: `whatsapp` | `email`).
  `messages.channel` column via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in
  `scripts/setup_db.sql`, with `whatsapp_message_id` generalized to an external id. Update
  `docs/database.md`.
- Reply dispatch per channel. The router and specialists stay unchanged; they only see
  `AgentRequest`.
- Related pending item: migrate the image expense path from OpenAI vision to Claude. This needs
  an eval baseline first.

### Phase 9 — Email ingestion worker (candidate for the Claude Agent SDK)

- Bank-notification emails via SES inbound or Gmail OAuth. Runs as a separate scheduled or
  queue-driven worker (container or separate Lambda), not in the webhook path.
- Per-bank parsing eval dataset; explicit consent and revocation flow; tokens stored in Secrets
  Manager / SSM.
- Natural place to adopt the Claude Agent SDK (PDF statements, multi-step reasoning, no
  latency pressure). See [design/open-challenges.md](design/open-challenges.md), Challenge 2.

---

## 7. Open questions / risks

- **Hosting (decided: Lambda).** Phases 5–8 run in the existing Lambda. If Phase 9 adopts the
  Agent SDK, that worker alone may need a container-image Lambda or a container; the chat path
  stays on the zip-based Lambda.
- **Routing (decided: hybrid, fast paths first, then router).** Watch for fast-path misroutes
  that need conversation context (e.g. a follow-up that happens to contain a report keyword).
  If they show up in evals or logs, narrow the fast paths rather than dropping them, and
  measure the cost before routing more traffic through the LLM.
- **Model refresh:** specialists and the router are on the Haiku tier. Re-run all evals before
  switching model versions.
- **Excel** still uses `parser.parse_report_request` (OpenAI) and doesn't share
  `reporting_agent`'s deterministic date resolution.
- **Proactive messages** (budget alerts, summaries) require WhatsApp templates outside the 24 h
  window. See [design/open-challenges.md](design/open-challenges.md), Challenge 1.
- **Deferred: users agent** (profile, category/budget management). It's dropped from this roadmap
  by decision, and can be added later as `delegate_to_users`.
