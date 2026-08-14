# Agentic Architecture — Master Plan (superseded past Phase 0)

> **Superseded.** [`docs/agent-architecture.md`](../agent-architecture.md) is now the
> source of truth for everything past Phase 0 — it uses Claude instead of OpenAI, and a
> different orchestrator/agent shape than the planner/handoff pattern below. **Phase 0 as
> described here is real, shipped work and is not retracted**: the `users`, `categories`,
> and `conversation_turns` tables it introduced are exactly what
> `docs/agent-architecture.md` builds on. Read this document for Phase-0 history and
> rationale; read the other one for what's actually being built next.

This is the **living design document** for evolving Finance Tracker from a stateless
expense logger into a conversational, multi-agent personal-finance assistant.

It is the single source of truth for *where we are going and why*. Each build phase
links back here, and each completed phase produces a **phase report** (see
[Documentation & review cadence](#documentation--review-cadence)).

Last updated: **Phase 0 complete** ([report](phase-reports/phase-0-foundation.md)). Phase 1 next.

---

## 1. Vision

A WhatsApp assistant, used by a small set of known people, that handles three financial
domains over a shared per-user database, plus cross-domain reporting:

- **Expenses** — conversational (create / correct / delete / undo), multimodal
- **Income** — multimodal now; Gmail/bank extraction later (see open challenges)
- **Personal** — customize categories, manage budgets, preferences
- **Reporting** — cross-domain summaries and Excel exports (read-only)

---

## 2. Current state (baseline)

| Aspect | Today |
|--------|-------|
| Flow | WhatsApp → Twilio → Lambda (`handler.py`) → OpenAI → Neon |
| Routing | Keyword heuristics (`is_excel_request`, `is_report_request`), else parse as expense |
| LLM calls | Direct single-shot (`parse_expense`, `parse_expense_from_image`, `transcribe_audio`, `parse_report_request`) — **no tool calling, no orchestration** |
| State | **Stateless** — every message independent, no memory |
| Identity | Phone-number allowlist; **no `users` table** |
| Data | 2 tables: `messages`, `expenses`. No incomes, no categories table (hardcoded in prompt), no budgets, no soft-delete |
| Quality infra | 34 tests passing; eval framework (`parse_expense`, `parse_report_request`); Phoenix tracing wired (`src/tracing.py`) |

**The gap:** no identity, no memory, no agents, no income/budget/category model.

---

## 3. Target architecture

### Agent roster

| Agent | Type | Responsibility |
|-------|------|----------------|
| **Orchestrator** | Planner + guardrails | Validate scope/safety, decide which specialist(s) handle the message, sequence them, compose one reply |
| **Expenses** | Writes | create / update / delete / undo expenses |
| **Income** | Writes | create / update / delete / undo incomes |
| **Personal** | Writes | category CRUD, budget CRUD, preferences |
| **Reporting** | Reads (cross-domain) | summaries (expenses + income + budgets), Excel export |

### Control flow (handoff + planner pattern)

```
Message (+ short history + user profile)
  │
  ▼
[ Guardrail: safety + scope ] ──reject──> polite decline
  │ ok
  ▼
ORCHESTRATOR / PLANNER → emits an ordered list of handoffs
  │   (N=1 fast path for single-domain; N>1 chains agents)
  ├── transfer_to_expenses
  ├── transfer_to_income
  ├── transfer_to_personal
  ├── transfer_to_reporting
  └── decline_out_of_scope
  │
  ▼  execute in order, feeding each result into the next
SPECIALIST AGENT(s) → pick tool(s) → execute against Postgres (scoped to user_id)
  │
  ▼
Reactive budget check → compose ONE reply → send + save turn
```

**Multi-agent chaining:** the orchestrator can call more than one agent for a single
message (e.g. *"crea la categoría 'mascota' y registra 30k ahí"* → Personal then
Expenses). Rules: respect dependency ordering, report partial failures honestly, and
always send a single composed reply.

### Guardrails (defense in depth)

| Layer | Catches | Mechanism |
|-------|---------|-----------|
| **Scope** | Off-topic ("write a poem") | Orchestrator `decline_out_of_scope` path |
| **Safety** | Prompt injection / jailbreak | Pre-check + hardened prompts |
| **Hard (code)** | Wrong-account access, data leakage | `user_id` is injected from the **Twilio-verified phone** — the LLM never chooses it. Tools physically cannot touch another user's data |
| **Destructive ops** | Accidental data loss | Soft-delete + `undo` for all deletes/edits |

> The hard, code-level guardrail is the floor: **never trust the model with
> authorization.** Every tool executor receives the resolved `user_id` from the handler.

---

## 4. Data model (target)

Schema is rebuilt cleanly in Phase 0 (existing data is disposable — confirmed).

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `users` | Identity | `id`, `phone` (unique), `name`, `prefs` (jsonb), `created_at` |
| `messages` | Raw inbound audit log | existing + `user_id` |
| `conversation_turns` | Short-term memory | `id`, `user_id`, `role` (user/assistant), `content`, `created_at` |
| `categories` | Custom per user | `id`, `user_id`, `name`, `kind` (expense/income), `active` |
| `expenses` | (existing) | + `user_id`, `deleted_at` (soft delete) |
| `incomes` | New, mirrors expenses | `user_id`, `amount`, `currency`, `category`, `income_date`, `source`, `description`, `confidence`, `deleted_at` |
| `budgets` | **Per-currency** budgets | `id`, `user_id`, `scope` (global / category), `currency`, `period` (monthly/weekly), `amount`, `active` |

**Key couplings:**
- Categories are **data** read into the expense/income agent prompts (so Personal-agent
  edits change parsing behavior).
- Budget math is shared by the reactive check **and** the future proactive batch — it
  lives in one module (`budgets.py`).
- Budgets are **per-currency** (a COP budget and a USD budget are independent).

---

## 5. Cross-cutting decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Models | `gpt-4o-mini` everywhere | Cheapest start; use Phoenix traces to find where a stronger model is actually needed, then upgrade selectively |
| Identity | Auto-provision `users` row from allowlisted verified phone | "Few known people" — no signup flow needed |
| Memory | `conversation_turns`, last N turns into context | Enables follow-ups ("change it" → "to 5000") |
| Deletes | Soft-delete + `undo` | Safety for financial data |
| Budget currency | Per-currency | User tracks COP and USD |
| Delivery | One feature branch + PR per phase | Reviewable, revertible |
| Observability | Phoenix trace review is part of every phase's exit criteria | Find bottlenecks early |

---

## 6. Phased plan

Each phase is independently shippable, ends with tests + evals green, a Phoenix trace
review, and a phase report.

### Phase 0 — Foundation & Memory
**Goal:** the data model and plumbing for everything; no user-visible behavior change.

- Rebuild schema: `users`, `categories`, `conversation_turns`, `incomes` (table only),
  `budgets` (table only); add `user_id` + `deleted_at` to `expenses`
- `get_or_create_user(phone)`; resolve `user_id` in the handler from the verified phone
- Seed default categories per new user
- Make `parse_expense` read the user's categories dynamically (first categories-as-data
  coupling)
- Conversation memory store (load/save turns)

**Evals/tests:** existing evals stay green; add a test that parsing uses dynamic
categories. **Exit:** schema in place, current behavior unchanged for the user, Phoenix
still tracing.

### Phase 1 — Orchestrator + Expenses + Reporting agents (the chatbot)
**Goal:** replace keyword routing with the planner/handoff orchestrator; conversational
expenses with corrections; reporting agent.

- `tools/expenses_tools.py` (create/update/delete/undo), `tools/reporting_tools.py`
  (get_report/export_excel)
- `agents/expenses_agent.py`, `agents/reporting_agent.py`
- `orchestrator.py`: guardrails → planner → ordered execution → composed reply
- Wire memory into context; new confirmation formats (update/delete/undo, multi-action)
- `handler.py` routes everything through the orchestrator (audio/image preprocessed first)

**Evals/tests:** NEW datasets — orchestrator routing (message → expected agent(s)),
expenses tool selection (message → tool + key args), correction/deletion cases. **Exit:**
corrections/deletions work, report/excel via agent, Phoenix shows
orchestrator→agent→tool spans, latency/cost reviewed.

### Phase 2 — Income agent
**Goal:** track income; reporting becomes net cashflow.

- `incomes` writes; `tools/income_tools.py`; `agents/income_agent.py`
- Orchestrator: `transfer_to_income` + expense/income disambiguation (asks when unsure)
- Reporting extended to income + net cashflow

**Evals/tests:** income parsing dataset; disambiguation dataset (message →
expense/income/ask). **Exit:** income works, reports show net, Phoenix review.

### Phase 3 — Personal agent + budgets + reactive warnings
**Goal:** user-customizable categories and per-currency budgets with reactive alerts.

- `budgets.py` (shared spend-vs-budget math, per currency, global + per-category)
- `tools/personal_tools.py` (category CRUD, budget CRUD, preferences);
  `agents/personal_agent.py`
- Orchestrator: `transfer_to_personal`
- Reactive warning appended to replies when an expense crosses a budget threshold

**Evals/tests:** personal-agent tool selection; budget math unit tests (offline);
reactive-trigger tests. **Exit:** per-currency budgets work, warnings fire reactively,
Phoenix review.

### Beyond Phase 3
Captured in [open-challenges.md](open-challenges.md): **proactive scheduled budget
alerts** and **Gmail/bank income extraction**. These are documented, not yet scheduled.

---

## 7. Documentation & review cadence

When a phase finishes, before merging the PR:

1. **Update reference docs** touched by the phase (`database.md`, `architecture.md`,
   `usage.md`, `evaluation.md` as applicable).
2. **Write a phase report** at `docs/design/phase-reports/phase-N-<name>.md` using the
   template below.
3. **Update this master plan's "Last updated" line** and tick the phase off.

### Phase report template

```markdown
# Phase N — <name> — Report

## What was built
- <modules, tables, tools, agents added/changed>

## Decisions made / changed
- <any deviation from this plan and why>

## Eval results
- <dataset(s), accuracy metrics, notable failures>

## Phoenix review (bottlenecks)
- Latency: p50 / p95 per agent + per tool
- Tokens & cost per message (avg)
- Slowest spans / hotspots found
- Actions taken or logged for later

## Follow-ups / tech debt
- <anything deferred>
```

### Phoenix's role (every phase)

Phoenix is the instrument we use to *see* the agentic system working and find where it's
slow or expensive. Each phase exit requires a trace review:

- Run the phase's evals and a handful of real messages with `PHOENIX_TRACING=1`
- Inspect the `finance-tracker` project: orchestrator → agent → tool span tree
- Record **latency (p50/p95), tokens, and cost per message**, and the slowest spans, in
  the phase report
- Multi-agent chaining and extra LLM hops will raise latency/cost — Phoenix is how we
  catch and justify (or fix) that

---

## 8. Working agreements

- One feature branch per phase (e.g. `feature/phase-0-foundation`), PR to `main`.
- A phase does not merge until: tests green, evals green, docs updated, phase report
  written, Phoenix reviewed.
- `user_id` always comes from the verified phone, never from the LLM.
- All deletes are soft; all destructive actions are undoable.
