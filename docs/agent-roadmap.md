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
| Agent contract | `src/finance_tracker/core/agent_types.py` — `AgentRequest` / `AgentResponse`, `handle(request) -> response` |
| Dispatch | `src/finance_tracker/routing/orchestrator.py` — deterministic keywords: report → income → expense default |
| Specialists | `src/finance_tracker/agents/expenses.py`, `src/finance_tracker/agents/incomes.py`, `src/finance_tracker/agents/reporting.py` (Claude Haiku, forced tool-use) |

### What's missing

- **Routing is keyword-only.** There's no LLM router, so off-topic text, multi-intent messages
  ("almuerzo 20k y me pagaron 2M") and ambiguous phrasing all fall through to the expense agent.
- **No guardrails** beyond the code-level ones (signature check, allowlist, `user_id` scoping).
- **No conversational corrections** ("no, eran 15000", "borra el último").
- **No user profile, onboarding or customization.** Every user is treated as a person tracking
  personal spending, and categories can only be changed in the database.
- **WhatsApp is the only input channel.**

### Goal

A **router (main) agent with guardrails** that sends each task to one or more specialists
(**expenses, incomes, reporting, users**), uses per-user conversation context **and the
user's profile**, and composes a single reply. The design should make it possible to add
non-WhatsApp sources (email) later without changing the agents.

The same message can mean different things for different users: "almuerzo 20 mil" is an
expense for a person, but it can be a sale (income) for a restaurant. Classification therefore
depends on the **user profile** (§5.1), which onboarding collects and the users agent maintains.

---

## 2. Framework decision: agents-as-tools on the `anthropic` SDK

We considered the Claude Agent SDK. The routing pattern we need (a router handing tasks to
subagents and composing their results) is **"agents-as-tools"**, and it's a short tool-use loop on
the `anthropic` SDK the project already uses.

| Concern | Agents-as-tools (`anthropic` SDK) | Claude Agent SDK |
|---|---|---|
| What it is | Your own loop over tools you define | Claude Code as a library: CLI subprocess, built-in file/bash/web tools, subagents, hooks, sessions |
| Fit for our specialists | Exact: specialists are structured extractors writing to Postgres | Most built-ins (file edit, bash, grep) are unused and must be disabled |
| Deployment | Unchanged: Lambda zip built by `scripts/deploy.py` | Container image (or long-running server), writable HOME, bundled CLI |
| Memory | Reuses `conversation_turns` | Has its own session store (local files), which would duplicate Postgres |
| Cost / latency per message | One small Haiku router call + the specialist call | Larger default prompt, multi-turn harness on every message |
| Guardrail control | Every tool executor is our Python; `user_id` injected by us | Possible via hooks, but through more layers |

### Confirmed decisions

| Decision | Choice |
|---|---|
| Hosting | **AWS Lambda** (zip deploy via `scripts/deploy.py`, run locally) |
| Routing order | **Hybrid:** deterministic keyword fast paths run first (personal profiles only); the LLM router handles everything they don't resolve |
| Framework | Agents-as-tools on the `anthropic` SDK (no Agent SDK in the request path) |
| Specialists | Expenses, incomes, reporting, **users** |
| User profile | `personal` or `business` (with business details), stored in the append-only `user_prefs` history (latest row wins); routing and classification depend on it (§5.1) |
| Onboarding | Fixed, non-blocking question flow on first use; answers interpreted by the users agent (§5.3) |
| Ambiguity | Ask, don't assume (§4.5) |

**Decision:** build the router and its loop on the `anthropic` SDK. Revisit the Agent SDK for
**Phase 10** (email ingestion / deeper analysis worker). That work is background, not
latency-sensitive, and benefits from a fuller harness.

---

## 3. Target architecture

```
WhatsApp ─► Twilio ─► handler.py  (UNCHANGED: signature verify → allowlist → get_or_create_user
                         │          → save_message (idempotent) → load_recent_turns → save_turn
                         │          → store_all_media → transcribe audio)
                         ▼
               onboarding (§5.3) ── pending question? ask it after handling the message
                         │
               orchestrator.handle_message(AgentRequest + profile)
                         │
          ┌──────────────┴───────────────┐
          │ keyword fast paths (free)    │  excel / report / income / simple-expense heuristic
          │ personal profiles only       │
          └──────────────┬───────────────┘
                         │ no confident match, or business profile
                         ▼
               router_agent.route(request)          ◄── guardrails (§6), profile context (§5.1)
                 tool-use loop (agent_loop.py, §4.3)
     ┌────────────┬────────────┬────────────┬────────────┬──────────────────┬──────────────────┐
     ▼            ▼            ▼            ▼            ▼                  ▼
 delegate_to_ delegate_to_ delegate_to_ delegate_to_ decline_out_of_scope ask_clarification
  expenses     incomes      reporting    users
     │            │            │            │
 agents.expenses agents.incomes agents.reporting agents.users   (handle(AgentRequest))
     └────────────┴────────────┴────────────┘
                         │ tool_results
                         ▼
               router composes ONE reply ─► handler.py sends via Twilio, save_turn
```

**Routing order is fixed: fast paths first, then the router.** The keyword fast paths stay
because they're free and already eval-covered. The router only pays for itself on messages the
keywords can't resolve confidently. The fast paths know nothing about the user, so they only run
for `personal` profiles; every message from a `business` profile goes to the router. Everything
runs inside the existing Lambda.

---

## 4. Router ↔ subagent contract

### 4.1 Tools the router sees

| Tool | Input | Executor |
|---|---|---|
| `delegate_to_expenses` | `{task: str}` | `agents.expenses.handle(child_request)` |
| `delegate_to_incomes` | `{task: str}` | `agents.incomes.handle(child_request)` |
| `delegate_to_reporting` | `{task: str}` | `agents.reporting.handle(child_request)` |
| `delegate_to_users` | `{task: str}` | `agents.users.handle(child_request)` (from Phase 6, §5.2) |
| `decline_out_of_scope` | `{reason: str}` | returns `config.MSG_OUT_OF_SCOPE` |
| `ask_clarification` | `{question: str}` | returns the question as the reply (no write) |

`task` is the router's rewrite of the relevant part of the user's message. For example,
"almuerzo 20k y me pagaron 2M" becomes two delegations: `"almuerzo 20000"` to expenses and
`"me pagaron 2000000"` to incomes. Adding a specialist later means one new `delegate_to_*` tool
plus one executor entry.

### 4.2 Executor rules

- The executor builds a **child `AgentRequest`**: same `user_id`, `message_id`, `phone`, `now`,
  `conversation`, `message_type` and `profile`, with `text=task`. The model never supplies `user_id`; no
  tool schema has a user field.
- On success it returns `reply_text` plus compact `data` (ids, amounts, categories) as the
  `tool_result`.
- If a specialist raises, the executor returns a `tool_result` with `is_error: true` and a short
  message. The router can then report partial success honestly ("registré el gasto, pero no pude
  registrar el ingreso"). `handler.py`'s top-level `try/except` → `MSG_ERROR` → `200` stays as the
  backstop.
- The router receives the bounded history from `load_recent_turns` so it can resolve follow-ups.
  Specialists keep receiving it through `AgentRequest.conversation`.

### 4.3 Agent loop semantics (`core/agent_loop.py::run_tool_loop`)

This is a shared helper. The router uses it from Phase 5, and any specialist that becomes
multi-step (the users agent in Phase 6, corrections in Phase 8) will use it too. Today's
specialists stay as a single forced-tool call and don't need a loop.

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
- **One deadline per message** (`core/deadline.py`): the handler starts it from the Lambda's
  remaining time minus `REPLY_RESERVE_SECONDS`, and every loop and Claude call in the message
  draws from it, nested loops included. A loop doesn't start a call with less than
  `CLAUDE_MIN_CALL_SECONDS` left, and each call's timeout (retry included) ends before the
  deadline, so the handler always has time to answer. It replaced fixed per-loop timeouts (12 s
  router, 8 s specialists) that stacked badly: on 2026-10-20 a settings change was applied, but
  both loops ran out before their closing calls and the user was told it had failed. A specialist
  that stops early still reports in code what it already did. Replies go out through Twilio's
  API, not the webhook response, so a message slower than Twilio's 15 s webhook timeout still gets
  its answer (Twilio logs error 11200; a retry is ignored because `save_message` is idempotent).
- **Observability:** stop reason, call duration and `response.usage` are logged per iteration, so logs and
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
  conjunction/income/report signal). Those messages skip the router. The heuristic must also
  see a clear expense word ("almuerzo 32000" qualifies; "nequi 50000" doesn't), because of the
  ambiguity policy below.

### 4.5 Ambiguity policy: ask, don't assume

**If the router is uncertain, it asks (`ask_clarification`) instead of assuming.** Examples:
the direction of the money is unknown ("nequi 50000", "transferencia juan 200k", "50000"), the
amount is missing ("almuerzo"), or the movement is neither an expense nor an income ("pasé 300
mil a ahorros"). The next message (e.g. "fue un gasto, mercado") is routed using the
conversation history. This replaces today's behavior of defaulting everything to the expense
agent. The fast paths must respect it too: a fast path may only fire when the intent is
unambiguous.

Ambiguity is judged **against the user's profile** (§5.1). "almuerzo 20 mil" is a clear expense
for a `personal` profile, but for a restaurant it could be a sale or the owner's own lunch, so
the router asks.

### 4.6 Routing eval and Phase 4 baseline

`evals/datasets/route_message.json` scores the routing decision only (`route`: agents /
clarify / decline / none, plus the set of agents). Specialists are never executed.

**Every case carries a `profile`** (§5.1), and the expected route is judged against it. The
same text can expect different routes for different profiles, e.g. "almuerzo 20 mil" → expenses
for a `personal` profile but clarify for a restaurant. The dataset has 64 cases: 46 `personal`
plus 10 `business` (a restaurant and a software consultancy) that are scored now, and 8
users-agent cases (`skip: phase-6`) and 1 correction case (`skip: phase-8`) that are scored once
those phases ship. The runner reports accuracy per profile (`profile:personal`,
`profile:business`) alongside the per-tag metrics.

`uv run python -m evals.run` prints the Phase 4 keyword-only baseline for free (no API calls,
55 scored cases):

| Metric | Phase 4 keyword routing |
|---|---|
| Overall | 27% |
| Personal / business profile | 24% / 40% |
| Single intent | 56% |
| Multi-intent, ambiguous, off-topic, small talk, adversarial | 0% (it can only pick one agent and never asks or declines) |
| Fast-path precision | **31%** (fires on 13 of 55 cases, right on 4) |

The business score is only 40% because of the expense default: today's code has no profile, so
it gets supply and payroll expenses right by accident and misses every sale, report and
ambiguous business message.

**Finding:** the report keyword check is far too broad to stay a "free and trusted" fast path
as-is. Single words and stems like `gastos`, `gasto de` and fuzzy matching make it fire on
multi-intent messages ("registra almuerzo 20k y dime cuánto llevo"), on adversarial ones ("borra
todos mis gastos", "muéstrame los gastos de +57…") and on clarification answers ("fue un gasto,
mercado"), and it then routes them to reporting with nothing behind it to correct the mistake.
Phase 5 must narrow the fast paths: report and income fast paths fire only on short,
single-intent messages and step aside when the text has a conjunction or an adversarial or
referential signal. Fast-path precision must reach ~100%, since there is no LLM behind a fast
path to catch errors.

---

## 5. User profile, users agent and onboarding

### 5.1 Profile class

Every user has exactly one current profile, stored under `prefs.profile` in the **append-only
`user_prefs` table**: every change inserts a new row with the full preferences snapshot and the
datetime, rows are never updated, and every read uses the user's latest row
(`database.get_user_prefs` / `database.append_user_prefs`, see
[database.md](database.md#user_prefs-append-only-preference-history)). The old `users.prefs`
column is deprecated and no longer read. `type` is `personal` or `business`. A business profile
adds the details the router needs to classify that business's money correctly.

```jsonc
// personal (the default for every existing and new user)
{"type": "personal"}

// business
{
  "type": "business",
  "business": {
    "name": "La Sazón de Ana",          // optional
    "industry": "restaurante",           // required
    "description": "Vende almuerzos ejecutivos y domicilios. Compra insumos, paga nómina y arriendo del local."  // required
  }
}
```

- `industry` and `description` are required for `business`: they say what the business **sells**
  (income) and what it **pays for** (expenses), which is exactly what classification needs.
  From Phase 6b, onboarding asks for these separately (`sells`, `main_costs`) and the router's
  profile summary is built from them; `description` stays readable for profiles set before.
- The profile `type` is **fixed** at the first onboarding (Phase 6 decision).
- Loaded once per message in `handler.py` (new `database.get_user_prefs(user_id)`) and passed as
  a new `AgentRequest.profile` field, so every agent sees the same profile.
- The router's system prompt gets a short, stable profile summary, e.g. *"Perfil: negocio —
  restaurante. Vende almuerzos ejecutivos y domicilios; compra insumos, paga nómina y arriendo."*
  It goes after the static instructions so the static part can still be prompt-cached.
- Classification rules by profile:

  | Profile | Clear income | Clear expense | Ask |
  |---|---|---|---|
  | `personal` | salary, client payments, refunds | food, transport, bills | direction unknown, no amount |
  | `business` | sales of what `description` says it sells | supplies, payroll, rent, services | anything that could be either a sale or the owner's personal spend ("almuerzo 20 mil" at a restaurant) |

- Keyword fast paths run only for `personal` (§3).
- **Not covered:** one account tracking both personal and business money. That needs tagging each
  record with which books it belongs to (§8).

Other keys that live next to `profile` in each `user_prefs` snapshot:

```jsonc
{
  "profile": {...},
  "default_currency": "COP",
  "report": {"default_period": "month", "group_by": "category"},
  "onboarding": {"step": "done", "completed_at": "2026-10-20T10:00:00-05:00"}
}
```

### 5.2 Users agent (`agents/users.py`)

> **Phase 6 scope is final.** Every decision and its reasoning is in
> [design/phase-6-users-onboarding.md](design/phase-6-users-onboarding.md). This section
> summarizes it.

A specialist like the other three (`handle(AgentRequest) -> AgentResponse`), reached through
`delegate_to_users`. It owns **the user and their settings**, never transactions: correcting an
expense or income stays in the expense and income agents (Phase 8). It's a separate agent rather
than tools on the router because:

- Its tools would otherwise be sent with **every** router call, including the many plain
  expenses. As an agent, they only load when someone changes a setting.
- Writes behind a delegation are less likely to happen by accident from a misread message.
- Settings changes are multi-step (validate, check affected records, confirm, apply), which fits
  `run_tool_loop` (§4.3).
- It can be tested and evaluated on its own.

**Capabilities**

| Area | Actions | Confirmation |
|---|---|---|
| Profile type | Fixed at the first onboarding; **not changeable** | — |
| Business details | Edit name, industry, what it sells, main costs | No |
| Main currency | Change (future records only) | Yes |
| Categories (expense / income) | Add; rename (past records follow via `category_id`); rename onto an existing name = merge; deactivate (never delete); reactivate by re-adding | Yes for rename/merge/deactivate with records |
| Help | "¿Qué puedes hacer?", how-to, list settings and categories | — |
| Out of scope | Account/data deletion, budgets, language change | Polite decline |

**Rules**

- **Destructive changes apply to the future; history stays.** Deactivated categories keep
  showing in past reports; a currency change only sets the default for new records. A rename
  is the exception by design: it relabels the same category everywhere.
- Records reference categories by **`category_id`** (Phase 6a), so a rename is one row and a merge
  re-points the records of the source category.
- Every category change is logged in the append-only **`category_events`** table; preference
  changes go through `database.append_user_prefs`.
- A pending confirmation is valid only for the very next message; anything but "sí" cancels it.
- Tools get `user_id` from the executor, never from the model, like every other tool.
- Names are validated: no duplicates per `kind`, reasonable length, normalized casing.
- **Spanish only** in this version.

### 5.3 Onboarding (`agents/onboarding.py`)

A **fixed, deterministic flow**, not an open-ended LLM chat, so it stays predictable, cheap and
testable. State lives in the latest `user_prefs` snapshot (`onboarding`).

- **Blocking, on the very first message.** Nothing is recorded until onboarding is finished or
  skipped, because the user may change the categories first. The first message is **held** and
  offered back at the end (*"Antes de empezar me escribiste '…'. ¿Lo registro?"*), keeping its
  original date. Only the first message is held.
- **Flow:** welcome message → personal or business (+ name, industry, what it sells, main costs)
  → main currency → expense categories (defaults or customize) → income categories (defaults or
  customize).
- **Answers:** text or voice notes. Closed questions use numbered options matched in code (no
  LLM). Only open answers go to the LLM (business details, category customization).
- `omitir` skips the current question (its default applies); `omitir todo` skips the rest.
- A message mid-onboarding gets a short answer and the current question again. An abandoned
  onboarding starts over from the beginning.
- `reiniciar configuración` (the whole message) reruns it **without the profile question** and
  saves a complete new snapshot (`source="onboarding_restart"`), so restarts can be counted.
- Personal and business profiles get **different fixed default categories**.
- No existing users to migrate: the database is rebuilt after Phase 6, so everyone onboards.

---

## 6. Guardrails (defense in depth)

| Layer | Catches | Mechanism |
|---|---|---|
| **Code (the floor)** | Cross-user access, data leakage | `user_id` only from the verified phone, injected by the executor; no tool takes a user id; no raw SQL from the model; all writes scoped by `user_id`; soft-delete only |
| **Input** | Abuse, oversized or empty input | Length cap; media-only / empty handling; user text treated as data in the system prompt (injection hardening) |
| **Scope** | Off-topic ("escríbeme un poema") | `decline_out_of_scope` → `config.MSG_OUT_OF_SCOPE` (Spanish) |
| **Loop** | Runaway cost / latency | `stop_reason`-driven loop + `max_iterations` + wall-clock budget + per-call token logging (§4.3) |
| **Settings writes** | Accidental or unwanted changes | Users agent confirms destructive/wide changes; deactivate instead of delete; renames are transactional (§5.2) |
| **Output** | Hallucinated numbers | The reply is composed from specialist `reply_text`s; the router may join or summarize but must not invent amounts. Checked in evals |

Unchanged and never weakened: `webhook.verify_signature`, the phone allowlist, and
`save_message` idempotency.

---

## 7. Phased rollout

Each phase follows the existing working agreements: its own branch and PR, tests and evals green,
docs updated, a phase report in `docs/design/phase-reports/`, and a Phoenix trace review covering
latency, tokens and cost per message.

### Phase 5 — Router agent (agents-as-tools), profile-aware

- New: `core/agent_loop.py` (§4.3) and `routing/router_agent.py` (tools + executors, §4.1–4.2).
- `orchestrator.handle_message`: keep the fast paths (narrowed per §4.6, `personal` only), add
  the simple-expense heuristic, and send everything else to `router_agent.route`.
- Multi-intent delegation, `decline_out_of_scope` and `ask_clarification`.
- **Profile-aware routing:** `database.get_user_prefs`, the new `AgentRequest.profile` field, and
  the profile summary in the router prompt (§5.1). Until Phase 6 a business profile is set
  directly in the database.
- `config.py`: `CLAUDE_ROUTER_MODEL`, `ROUTER_MAX_ITERATIONS`, `AGENT_LOOP_TIMEOUT_SECONDS`,
  `MSG_OUT_OF_SCOPE`.
- **Evals:** `evals/datasets/route_message.json` with a `profile` per case (§4.6), scored by
  `score_routing`; the router runs with specialists stubbed. Thresholds set from the first live
  run, including per-profile accuracy.
- **Tests:** `tests/core/test_agent_loop.py` with mocked Claude sequences covering:
  tool_use → tool_use → end_turn; reaching `max_iterations`; `max_tokens` with a partial
  tool_use; refusal; timeout; a specialist raising (→ `is_error` result, loop continues).
  Also `tests/routing/test_router_agent.py` for executor wiring, the `user_id` injection invariant, and
  "business profile never uses the fast paths".
- **Exit:** routing eval above threshold for both profiles; existing expense/income/report evals
  unchanged; Phoenix shows router → specialist spans.

**As built** (`feature/phase-5-router-agent`; report: [phase-5-router-agent.md](design/phase-reports/phase-5-router-agent.md)). Live eval: router alone and hybrid both **100%** on 55 cases, two runs; router p50 2.5 s. Phoenix review pending.

- **Router model:** `claude-haiku-5-5` at `effort: "low"` (`ROUTER_MAX_TOKENS=2048`, since thinking
  counts toward it). The specialists stay on `claude-haiku-4-5-20251001` with forced tool use.
- **Fast paths:** one function, `fast_paths.fast_route(text, profile, conversation)`, used by both
  `handler.py`'s report branch and the orchestrator. It fires only for `personal` profiles, never
  when the last assistant turn was a question, and never on text with a conjunction, `", "`,
  `;`, a destructive or injection word, or a phone number. Report: phrase match, or a single
  report word in a message of ≤3 words (no fuzzy matching). Income: income phrase + an amount.
  Expense: an amount + a clear expense word in ≤7 words. On `route_message.json` it fires on
  9 of 55 cases and is never wrong; `tests/routing/test_fast_paths.py` enforces that.
- **Reply assembly in code:** specialist `reply_text`s are sent verbatim, followed by the
  clarifying question; `decline` sends `MSG_OUT_OF_SCOPE`; the router's own text is used only
  when no tool ran (small talk). A tripped safeguard lists what was done plus
  `MSG_PARTIAL_TIMEOUT`, or sends `MSG_ERROR` when nothing was done.
- **Input:** conversation and message go in one user turn as tagged data
  (`<conversation>…</conversation><message>…</message>`), capped at `ROUTER_MAX_INPUT_CHARS`.
- **Limits:** `ROUTER_MAX_ITERATIONS=6`, `ROUTER_MAX_DELEGATIONS=3`,
  `AGENT_LOOP_TIMEOUT_SECONDS=12` (later replaced by one deadline per message, §4.3).
- **Preferences:** profiles are read from the append-only `user_prefs` table (latest row,
  §5.1); `database.append_user_prefs` is the only writer, ready for Phase 6.
- **Evals:** `uv run python -m evals.run --only routing` runs the keyword baseline, the router
  alone and the hybrid (production) path with only `ANTHROPIC_API_KEY` set.
- **Tests:** `tests/conftest.py` now fails any test that reaches a real Claude or OpenAI client.

### Phase 6 — Users agent + onboarding

Scope and decisions: [design/phase-6-users-onboarding.md](design/phase-6-users-onboarding.md).
Delivered as three PRs:

- **6a — Category ids (refactor, no behavior change).** `expenses.category_id` /
  `incomes.category_id` referencing `categories(id)` replace the `category` text column (no
  migration: the database is rebuilt from `scripts/setup_db.sql` after Phase 6). Reads join
  `categories` for the name. New append-only `category_events` table. Business default
  categories in `config.py`. Current tests and evals stay green.
- **6b — Onboarding.** `agents/onboarding.py` state machine (§5.3); held first message;
  `omitir` / `omitir todo` / `reiniciar configuración`; profile-based category seeding.
  **Built** — details in the design doc §2.7. Deterministic unit tests for every step.
- **6c — Users agent.** `agents/users.py` (§5.2) on `run_tool_loop`; `delegate_to_users` in the
  router; settings functions in `database.py` scoped by `user_id`; confirmation flow; help.
  Enables the `skip:"phase-6"` cases in `route_message.json` and adds a users-agent dataset
  (message → expected tool + arguments, including confirmation-required cases).
  **Built** — design doc §3.3. Live evals: users agent 100% (n=18), routing 100% (n=66).
- **Exit:** a new user can complete or skip onboarding as personal or business; categories,
  currency and business details can be changed from chat and take effect on the next message;
  renames show up in past reports, deactivated categories stay in them.

### Phase 7a — Reply context (WhatsApp swipe-replies)

Design and decisions: [design/phase-7a-reply-context.md](design/phase-7a-reply-context.md).
Done before Phase 8 because a swipe-reply is the most natural way to point at the record to
correct.

- Read `OriginalRepliedMessageSid` / `OriginalRepliedMessageSender`; resolve the quoted
  message from our database (inbound by SID; outbound SIDs now stored in
  `conversation_turns.external_id`), Twilio `fetch()` only as a fallback.
- Deterministic link to records: bot turns store the inbound `message_id` they answered, and
  records already carry `message_id`.
- `AgentRequest.replied_to`, passed to the router as tagged data; fast paths skipped for
  replies; onboarding and pending confirmations ignore it.
- **Evals:** routing cases with `replied_to`.

### Phase 7 — Guardrails hardening

- Adversarial and off-topic eval cases (prompt injection, "ignore previous instructions",
  requests for another user's data, non-finance chat).
- Input length cap and empty-message handling; per-message token/cost logging summarized in the
  phase report.
- **Exit:** adversarial dataset passes; there's no path where model output reaches SQL or
  chooses `user_id`.

### Phase 8 — Conversational corrections

Scope and decisions: [design/phase-8-corrections.md](design/phase-8-corrections.md).

- **Edit, delete and undo** expenses and incomes from the chat, inside the expense and income
  agents (each for its own records; not a new agent). Records are referenced by swipe-reply
  (exact, via Phase 7a reply context), "the last one", or a description ("el Uber de ayer");
  more than one match → ask.
- **Every field** can change; `created_at` never does (audit). A **kind change** ("eso era
  un ingreso") is a soft delete in one table plus a create in the other, linked in the log.
- **Ask until sure:** an edit through a swipe-reply applies directly and shows what changed;
  with "the last one" or a description the agent confirms the target record first.
  **Deletes ask for confirmation; undo always clarifies first** (no fixed "how far back"
  limit, and an undo can't be undone); **bulk deletes are declined**. A kind change needs no
  confirmation when the record is exact. Confirmations reuse the Phase 6c `pending_action`
  mechanism.
- Description search covers the last 60 days; Excel gets an "Ingresos" sheet and a summary
  with the net.
- **Soft delete** (`deleted_at`) only. **Append-only `record_events`** log with before/after
  values for every create, edit, delete, undo and move: the audit trail and the basis of undo.
- **Reports:** expenses, then incomes, then the net, per currency; Excel gets incomes.
- The agents' loop ends only when Claude ends its turn: a plain new record costs one
  extra (short) closing call.
- **Delivery:** 8a edit log + reports with incomes (**built**); 8b edit and delete
  (**built**); 8c undo and kind changes.
- **Evals:** `corrections.json` (action, target record, final values, confirmation asked);
  the skipped correction routing cases enabled.

### Phase 9 — Channel abstraction

- Normalize inbound into a channel-agnostic message (`channel`: `whatsapp` | `email`).
  `messages.channel` column via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in
  `scripts/setup_db.sql`, with `whatsapp_message_id` generalized to an external id. Update
  `docs/database.md`.
- Reply dispatch per channel. The router and specialists stay unchanged; they only see
  `AgentRequest`.
- Related pending item: migrate the image expense path from OpenAI vision to Claude. This needs
  an eval baseline first.

### Phase 10 — Email ingestion worker (candidate for the Claude Agent SDK)

- Bank-notification emails via SES inbound or Gmail OAuth. Runs as a separate scheduled or
  queue-driven worker (container or separate Lambda), not in the webhook path.
- Per-bank parsing eval dataset; explicit consent and revocation flow; tokens stored in Secrets
  Manager / SSM.
- Natural place to adopt the Claude Agent SDK (PDF statements, multi-step reasoning, no
  latency pressure). See [design/open-challenges.md](design/open-challenges.md), Challenge 2.

---

## 8. Open questions / risks

- **Hosting (decided: Lambda).** Phases 5–9 run in the existing Lambda. If Phase 10 adopts the
  Agent SDK, that worker alone may need a container-image Lambda or a container; the chat path
  stays on the zip-based Lambda.
- **Routing (decided: hybrid, fast paths first, then router).** Watch for fast-path misroutes
  that need conversation context (e.g. a follow-up that happens to contain a report keyword).
  If they show up in evals or logs, narrow the fast paths rather than dropping them, and
  measure the cost before routing more traffic through the LLM.
- **Model refresh:** specialists and the router are on the Haiku tier. Re-run all evals before
  switching model versions.
- **Excel** still uses `legacy.openai_parser.parse_report_request` (OpenAI) and doesn't share
  `reporting_agent`'s deterministic date resolution.
- **Proactive messages** (budget alerts, summaries) require WhatsApp templates outside the 24 h
  window. See [design/open-challenges.md](design/open-challenges.md), Challenge 1.
- **Personal and business money in one account.** A business owner who also logs personal
  spending would get mixed reports. Solving it means tagging each record with which books it
  belongs to (e.g. a `ledger` column) and letting reports filter by it. Not planned yet.
- **Category ids vs text (decided: ids, Phase 6a).** Records reference `categories(id)`.
- **Budgets** (the `budgets` table already exists) aren't covered by the users agent yet. They're
  a natural later addition to it.
