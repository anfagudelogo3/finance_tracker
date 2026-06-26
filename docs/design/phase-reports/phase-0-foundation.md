# Phase 0 — Foundation & Memory — Report

Status: **complete** · Branch: `feature/phase-0-foundation`

Goal: lay the data model and plumbing for the multi-agent system without changing what
the user experiences. See [agentic-architecture.md](../agentic-architecture.md).

## What was built

**Schema** ([`scripts/setup_db.sql`](../../../scripts/setup_db.sql)) — rebuilt around a
`users` entity:
- New tables: `users`, `conversation_turns`, `categories`, `incomes`, `budgets`
- `expenses`: added `user_id` and `deleted_at` (soft delete)
- `messages`: added `user_id`
- `incomes` and `budgets` are created now but exercised by later phases (2 and 3)
- A commented RESET block at the top (existing data is disposable)

**Config** ([`src/config.py`](../../../src/config.py)):
- `DEFAULT_EXPENSE_CATEGORIES`, `DEFAULT_INCOME_CATEGORIES` (seeded per new user)
- `CONVERSATION_MAX_TURNS` (8), `CONVERSATION_WINDOW_MINUTES` (30) — the memory window knobs

**Database** ([`src/database.py`](../../../src/database.py)):
- `get_or_create_user(phone)` — auto-provisions a user + seeds default categories on first contact
- `get_user_categories(user_id, kind)` — categories-as-data, with default fallback
- `save_turn` / `load_recent_turns` — conversation memory store (time-windowed, capped, returned chronological)
- `save_message`, `save_expense`, `get_expenses` now scoped by `user_id`; `get_expenses` excludes soft-deleted rows

**Parser** ([`src/parser.py`](../../../src/parser.py)):
- `SYSTEM_PROMPT` constant replaced by `_build_system_prompt(categories)` — the user's
  categories are injected per request; `parse_expense` / `parse_expense_from_image` accept
  an optional `categories` argument (defaults preserve old behavior)

**Handler** ([`src/handler.py`](../../../src/handler.py)):
- Resolves `user_id` from the verified phone (the **hard guardrail** — the LLM never
  chooses the account)
- Threads `user_id` through saves and reads
- Loads the user's categories into the parser calls
- Records inbound and outbound turns into conversation memory

## Decisions made / changed

- **No behavior change for the user** in this phase — keyword routing (`is_excel_request`,
  `is_report_request`) is untouched; it's replaced by the orchestrator in Phase 1.
- Conversation memory is **recorded** now but not yet **consumed** by prompts — the
  consumer is the orchestrator (Phase 1). Recording early means real history accumulates
  before the chatbot needs it.
- `incomes.message_id` uses `ON DELETE SET NULL` (income may later come from Gmail/bank
  with no originating message), unlike `expenses.message_id` which cascades.

## Eval results

- **Offline test suite: 45 passed** (up from 34). New coverage: user provisioning +
  category seeding, category fetch with fallback, conversation save/load ordering,
  dynamic category injection into the prompt, and the prompt builder.
- **Live evals (OpenAI API): not run this phase** — the local `OPENAI_API_KEY` is a
  placeholder. They remain valid by construction: `parse_expense` with no categories uses
  the same default list as before, so `evals/datasets/parse_expense.json` is unaffected.
  Run `uv run python -m evals.run` once a real key is set to confirm.

## Phoenix review (bottlenecks)

- **Phase 0 introduces no new LLM calls and no new agents**, so there is nothing new to
  trace yet. The existing `parse_expense` / `parse_report_request` spans are unchanged.
- Tracing wiring is intact (`setup_tracing()` still called at handler import; no signature
  changes to the traced OpenAI calls).
- **Full Phoenix bottleneck review begins in Phase 1**, when the orchestrator adds the
  `orchestrator → agent → tool` span tree and multi-agent hops that actually affect
  latency and cost.

## Follow-ups / tech debt

- Apply the new schema to the Neon database (run the RESET block + `setup_db.sql`) before
  deploying — this phase changes table shapes.
- `architecture.md` / `usage.md` describe the pre-agent flow; they will be rewritten in
  Phase 1 when routing actually changes.
- Conversation-turn recording in the current handler is lightweight; it will be
  centralized in the orchestrator in Phase 1.
