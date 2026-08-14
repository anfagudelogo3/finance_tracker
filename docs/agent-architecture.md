# Agent Architecture — source of truth

**Status:** Phase 1 (contract + expense agent, text/audio only) implemented on
`feature/phase-1-expense-agent`. This document is the canonical design for evolving
Finance Tracker from a linear parser pipeline (`handler.py` → keyword routing →
`parser.py` OpenAI calls) into a hand-rolled orchestrator + specialist-agent architecture
on Claude models. Twilio ingestion, signature verification, and the Lambda entry point
are unchanged — this is entirely about what happens after `handler._handle_message` has a
verified, authorized, saved message in hand. See [architecture.md](architecture.md) for
the pipeline this replaces.

**Relationship to prior design work:** an earlier, separate design effort
(`docs/design/agentic-architecture.md`) shipped a real "Phase 0" foundation directly on
`main` before this document existed: a `users` table (identity, auto-provisioned from the
verified Twilio phone — no `customers` table or onboarding gate needed), a `categories`
table (data-driven, injected into extraction prompts), and a `conversation_turns` table
(recorded on every inbound/outbound message, not yet consumed by any prompt). That work is
real and kept — this document builds on it rather than re-proposing it. `docs/database.md`
documents the current schema in full; `docs/design/agentic-architecture.md` and
`docs/design/open-challenges.md` are superseded by this document for everything past their
Phase 0 (see the banner on those files).

**Deviations from this document as originally drafted, decided during Phase 1
implementation:**
1. No guardrail/off-topic classification call yet — `handler.py`'s existing
   `is_excel_request`/`is_report_request` keyword gates already run before the
   orchestrator is ever called, and there's no off-topic deflection behavior in the
   product today to preserve. Adding a classification call now would be a new LLM call
   with nothing to gate. It lands once a second agent (income) exists to route between.
2. Image expenses stay on the old OpenAI vision path (`parser.parse_expense_from_image`),
   untouched, for now — no eval/test baseline exists for that path to diff against.
   Text and audio-transcript expenses (audio reduces to text after Whisper) moved to
   Claude; image migrates in a later slice.
3. `handler.py`, not the orchestrator, still performs the actual Twilio send for the
   expense branch — matching how the excel/report branches already work, rather than
   centralizing sends in the orchestrator as §1's diagram shows. That consolidation makes
   more sense once the orchestrator handles every branch, not just expense.
4. Agent-level exceptions are not caught into `AgentResponse(ok=False, ...)` — they
   bubble to `handler.py`'s existing top-level `try/except`, exactly like every OpenAI
   call in `parser.py` today (zero internal error handling), preserving the current error
   contract rather than introducing new agent-level error-handling behavior.

## 1. Overview

An **orchestrator** dispatches each inbound message to one of four specialist agents:
**onboarding**, **expense**, **income**, and **reporting**. Every agent implements the
same request/response contract and returns a fully-composed reply. Every LLM call in the
new architecture runs on Claude, with one deliberate, permanent exception: audio
transcription stays on OpenAI Whisper, because Claude has no speech-to-text endpoint.
User identity is resolved via the `users` table (`database.get_or_create_user`, shipped in
Phase 0) — not a per-agent concern.

```
                    ┌───────────────────────────── AWS Lambda ─────────────────────────────┐
                    │                                                                        │
 WhatsApp  ── POST ─┼─▶ handler.py (unchanged)                                               │
    ▲               │     ├─ verify Twilio signature                                         │
    │               │     ├─ allowlist check                                                 │
    │               │     ├─ get_or_create_user(phone) ──────────────────► Neon (users)      │
    │               │     ├─ save_message (idempotent) ──────────────────► Neon (PG)         │
    │               │     ├─ save_turn("user", ...) ─────────────────────► Neon (conv. turns) │
    │               │     └─ store_all_media ───────────────────────────► S3                 │
    │               │              │                                                          │
    │               │              ▼                                                          │
    │               │        keyword pre-filter (excel / report — unchanged, handler.py)      │
    │               │              │  (neither matched → expense, the only routed case today) │
    │               │              ▼                                                          │
    │               │        orchestrator.handle_message(AgentRequest)                        │
    │               │              └─ expense_agent (Claude Haiku, text/audio only) ─┐         │
    │               │                                                               ▼         │
    │               │                                                  Neon (expenses)         │
    │  reply         │              AgentResponse.reply_text ──► handler.py sends via Twilio    │
    └───────────────┼──────────────────────────────────────────────────────────────────────────┘
                    └────────────────────────────────────────────────────────────────────────┘
```

This is Phase 1's actual shape — see the deviations above for why it's narrower than the
guardrail/multi-agent diagram this document originally sketched (still the target for
later phases, once income/report/off-topic classification exist to route between).

Audio path: `transcribe_audio` (OpenAI Whisper) runs before the orchestrator sees the
message, exactly as today — its output is just transcript text, so from the
orchestrator's point of view an audio message and a text message are identical.

## 2. Model provider change: OpenAI → Claude

### Per-agent model tiers

| Agent / call | Model | Status | Why |
|---|---|---|---|
| Expense agent — text/audio-transcript extraction | `claude-haiku-4-5-20251001` | **Shipped, Phase 1** | Mirrors the old `gpt-4o-mini` tier: narrow, high-volume, cost-sensitive structured extraction from short WhatsApp messages. Forced tool-use (`tool_choice`), not prompt-described JSON — see below. |
| Orchestrator — guardrail + intent classification | `claude-haiku-4-5` | Planned | Deferred per deviation 1 above — no second agent to route between yet, so no call is made. |
| Onboarding agent — parse name / email / category preferences | `claude-haiku-4-5` | Planned, needs revisit | See §6 — the `users` table already auto-provisions with no gate, so this section's premise needs updating before it's actually built. |
| Expense agent — receipt/photo extraction | `claude-sonnet-5` | Planned | Deferred per deviation 2 above — stays on `parser.parse_expense_from_image` (OpenAI) until a vision eval baseline exists. |
| Income agent — text / photo | `claude-haiku-4-5` (text), `claude-sonnet-5` (photo/deposit-slip) | Planned | Structurally identical extraction task to expense, just a different category enum; builds against the already-existing `incomes` table (see §5). |
| Reporting agent — date-range parsing | `claude-haiku-4-5` | Planned | Same narrow, deterministic-ish JSON shape as today's `parse_report_request` (`{min_date, max_date}`). |
| Reporting agent — report *formatting* | No LLM call | Unchanged | `reporting.format_report`'s existing grouping/totals logic stays deterministic Python — it's not a language task, and keeping it out of the LLM avoids hallucinated numbers in a place where correctness matters most. |
| Audio transcription | OpenAI Whisper (`whisper-1`) — **unchanged** | Shipped (was never touched) | Claude has no ASR endpoint. This is a permanent, intentional exception, not a temporary gap — see the callout below. |

> **Future tier, not now:** if the reporting agent later grows narrative/analytical
> summaries ("you're spending 20% more on comida than last month") beyond today's grouped
> totals, that's a stronger reasoning task and should move to `claude-sonnet-5`. Nothing
> in this proposal requires that yet.

### The audio exception

Anthropic does not offer a speech-to-text endpoint comparable to OpenAI's Whisper.
Keeping `transcribe_audio` on OpenAI (`OPENAI_API_KEY`, `OPENAI_AUDIO_MODEL = "whisper-1"`)
is therefore not a partial migration — it's the correct end state. No other code path
touches OpenAI once this proposal is fully rolled out; `transcribe_audio` remains the
single, clearly-scoped exception. A non-blocking future option worth noting: since the
Lambda already has AWS credentials and `boto3`, **AWS Transcribe** would remove the OpenAI
dependency entirely. That's out of scope here — it would need its own accuracy comparison
against Whisper on Spanish/Colombian-slang audio before being worth the switch.

### `config.py` changes

Shipped in Phase 1 — only what the expense agent needs, added alongside the existing
OpenAI constants (not replacing them; excel/report/image still use `OPENAI_TEXT_MODEL`/
`OPENAI_VISION_MODEL`):

```python
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
```

`CLAUDE_CLASSIFIER_MODEL`, `CLAUDE_VISION_MODEL`, `CLAUDE_REPORT_MODEL` are still planned,
not yet added — they land with the agents that need them (orchestrator classification,
image expense, report date-range parsing). `OPENAI_TEXT_MODEL`/`OPENAI_VISION_MODEL` are
not removed — image expense and report/excel parsing still depend on them.

`src/claude_client.py` — a module-scope singleton, mirroring how `parser.py` does
`client = OpenAI(api_key=OPENAI_API_KEY)`:

```python
import anthropic
from config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
```

**Structured outputs via forced tool-use, not `output_config.format`/`messages.parse()`.**
Today's `parser.py` describes the expected JSON shape in the system prompt and calls
`json.loads(content)` with no error handling — a malformed response is an unhandled
exception that surfaces as `MSG_ERROR`. `expense_agent.py` closes that gap with a
`record_expenses` tool (JSON-schema `input_schema`, including a `category` enum built from
the user's actual categories) and `tool_choice={"type": "tool", "name": "record_expenses"}`
— the API validates the response against the schema before it ever reaches Python, and it
doesn't depend on a newer structured-output beta feature that may not exist in the pinned
SDK version. Later agents should follow the same pattern.

### Evals framework changes

| File | Change | Status |
|---|---|---|
| `evals/__init__.py` | Bootstrap dummy `ANTHROPIC_API_KEY` the same way `OPENAI_API_KEY` is bootstrapped. | Shipped |
| `evals/run.py` | New `_run_expense_agent()`, sharing scoring/aggregation with `_run_parse_expense()` via `_run_expense_dataset()`. Runs against the same `parse_expense.json` dataset, printed side by side. Skips (with a clear message) instead of failing when `ANTHROPIC_API_KEY` isn't a real key, so `--check` doesn't spuriously fail for developers who haven't set it. | Shipped |
| `pyproject.toml` (`evals` group) | Added `openinference-instrumentation-anthropic` alongside `openinference-instrumentation-openai`. | Shipped |
| `src/tracing.py` | `setup_tracing()` gained `AnthropicInstrumentor().instrument()` alongside `OpenAIInstrumentor().instrument()`. | Shipped |
| `evals/run.py` → `THRESHOLDS["expense_agent"]` | **Not yet set.** No `ANTHROPIC_API_KEY` was available in the environment Phase 1 was implemented in, so the live Claude-vs-OpenAI comparison hasn't been run. Run `uv run python -m evals.run` with a real key, observe the numbers, then add a `THRESHOLDS["expense_agent"]` entry (with headroom) before relying on `--check` to gate this path. | **Pending — do this before treating Phase 1 as fully verified.** |
| `evals/scoring.py` | `score_expense` is reusable as-is for income (Phase 2) — same shape, only the category enum differs. | Unchanged, still applies |
| `evals/datasets/` | New `parse_income.json` (Phase 2); optionally `classify_intent.json` once the orchestrator's guardrail/classifier exists (Phase 2+, see deviation 1). | Planned |

## 3. Orchestrator ↔ agent contract

All four agents — onboarding, expense, income, reporting — implement the same function
signature:

```python
def handle(request: AgentRequest) -> AgentResponse: ...
```

### `AgentRequest` / `AgentResponse` — as implemented, `src/agent_types.py`

Reuses existing dict shapes instead of inventing parallel types for things that already
have one: `media` matches `media.store_all_media()`'s output, `conversation` matches
`database.load_recent_turns()`'s `{role, content}` rows. No `Customer`/`PresignedFile`
dataclasses — identity is a plain `user_id` (the `users` table already exists), and
attachments are a plain dict.

```python
@dataclass
class AgentRequest:
    user_id: int
    message_id: int
    phone: str
    text: str                  # message body, or Whisper transcript for audio
    media: list[dict]          # media.store_all_media() shape; [] for text/audio
    message_type: str          # "text" | "audio" | "image"
    now: str                   # ISO datetime, America/Bogota
    categories: list[str]      # database.get_user_categories(user_id, "expense")
    conversation: list[dict]   # [] in Phase 1 — recorded since Phase 0, not yet consumed


@dataclass
class AgentResponse:
    ok: bool
    reply_text: str                        # Spanish, ready to send as-is
    reply_attachment: dict | None = None   # {"url": str, "filename": str}; unused so far
    data: list[dict] | None = None         # persisted row(s), incl. expense_id, for logging
    error: str | None = None               # unpopulated in Phase 1, see deviation 4
```

`whatsapp.format_confirmation` (a pure formatting function, not a network call) is reused
by `expense_agent.py` to build `reply_text` — the "agents never call `whatsapp.py`" framing
below is really about not letting agents bypass the single-send point, not about reusing
pure string formatters; noting the distinction since the original wording was stricter than
what's implemented.

In Phase 1, `handler.py` — not the orchestrator — still performs the actual
`whatsapp.send_message` call and `save_turn`, matching how the excel/report branches
already work (see deviation 3 above).

### Worked example (as implemented)

Input message: `"uber 14000"` from an existing user.

```json
// AgentRequest passed to orchestrator.handle_message(...) → expense_agent.handle(...)
{
  "user_id": 7,
  "message_id": 142,
  "phone": "+573001234567",
  "text": "uber 14000",
  "media": [],
  "message_type": "text",
  "now": "2026-08-13T20:10:00-05:00",
  "categories": ["comida", "transporte", "mercado", "salud", "entretenimiento",
                 "hogar", "educacion", "ropa", "servicios", "otro"],
  "conversation": []
}
```

```json
// AgentResponse returned by expense_agent.handle(...)
{
  "ok": true,
  "reply_text": "✅ Registré COP 14.000 en transporte para 2026-08-13",
  "reply_attachment": null,
  "data": [{"amount": 14000, "currency": "COP", "category": "transporte",
            "payment_method": null, "merchant": null, "description": "viaje en Uber",
            "date": "2026-08-13", "confidence": 0.8, "source": "text", "expense_id": 42}],
  "error": null
}
```

### Orchestrator responsibilities — Phase 1 as shipped vs. target

1. `get_or_create_user(phone)` — **not an orchestrator step**; already resolved by
   `handler.py` before the orchestrator is ever called (Phase 0, shipped).
2. ~~If onboarding isn't complete, dispatch to `onboarding_agent`~~ — no onboarding gate
   exists; `get_or_create_user` auto-provisions. See §6 for whether/how a welcome chat
   still makes sense.
3. Keyword pre-filter for Excel export / report requests — unchanged from today's
   `is_excel_request` / `is_report_request`, evaluated in `handler.py` before the
   orchestrator is called at all (not inside `orchestrator.py`).
4. ~~Guardrail + intent classification~~ — deferred, see deviation 1. Today
   `orchestrator.handle_message` dispatches directly to `expense_agent`, the only agent
   that exists.
5. ~~`on_topic: false` → deflection reply~~ — not implemented yet (nothing to guard
   against with a single agent and no off-topic case in the product today).
6. Dispatch to the matching agent, send `reply_text` — done, but the actual Twilio send
   is still in `handler.py` per deviation 3, not centralized in the orchestrator.
7. (Phase 4) Persist both turns to conversation history — the *storage* already exists
   and both turns are already recorded (`handler.py` calls `save_turn` before and after
   dispatch, shipped in Phase 0); Phase 4 is "read it back into agent prompts", not
   "build it."

Because the keyword pre-filter for Excel/report still runs before the orchestrator,
export and report intents remain **free** (no Claude call) for the common case — this
still holds even though the classification call itself hasn't been built yet.

## 4. Module layout under `src/`

Modules stay **flat** — no `src/agents/` subpackage. `scripts/deploy.sh` runs
`cp src/*.py build/`, which is non-recursive; a subpackage would need that script updated
before any of this could deploy. Staying flat means this proposal needs zero changes to
`deploy.sh`.

| Module | Responsibility | Status |
|---|---|---|
| `claude_client.py` | Module-scope Anthropic client singleton, mirrors `parser.py`'s OpenAI client | Shipped |
| `agent_types.py` | Shared `AgentRequest` / `AgentResponse` dataclasses — the contract above | Shipped |
| `orchestrator.py` | Single-agent dispatch today; grows keyword-independent routing once a second agent exists | Shipped (minimal) |
| `expense_agent.py` | Identify, extract, classify, and persist an expense record — **text + audio-transcript only** | Shipped (text/audio) |
| `income_agent.py` | Identify, extract, classify, and persist an income record (text + photo), against the already-existing `incomes` table | Planned (Phase 2) |
| `reporting_agent.py` | Date-range parsing (Claude) + report formatting (deterministic, delegates to `reporting.py`) | Planned |
| `onboarding_agent.py` | Welcome chat — see §6, needs its premise updated before this is built | Planned, needs revisit |

No `customers.py` — identity already lives in `database.py` (`get_or_create_user`,
`get_user_categories`, Phase 0). No `conversation.py` — conversation storage already lives
in `database.py` (`save_turn`, `load_recent_turns`, Phase 0); Phase 4 just means reading it
into agent prompts.

`parser.py`, `database.py`, `reporting.py`, `excel.py`, `whatsapp.py`, `media.py`,
`webhook.py` keep their current single responsibilities. `handler.py`'s Phase 1 change:
the excel/report keyword branches and the image-expense branch are untouched; the
text/audio-expense branch now builds an `AgentRequest` and calls
`orchestrator.handle_message` instead of calling `parser.parse_expense` directly.

## 5. Schema changes

**`users`, `categories`, `conversation_turns` already exist** — shipped in Phase 0
(`docs/design/phase-reports/phase-0-foundation.md`), documented in full in
`docs/database.md`. No `customers` table is needed; identity, per-user categories, and
conversation-turn storage are already solved problems by the time this document's phases
start. The subsections below describe what's still genuinely new.

### `incomes` (already exists — Phase 0 created the table; Phase 2 adds the agent)

Mirrors `expenses`' shape (separate table, not a `type` discriminator column, so
`category` keeps meaning one thing per table) — this table already shipped in
`scripts/setup_db.sql`, unused until the income agent lands:

```sql
CREATE TABLE IF NOT EXISTS incomes (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category        VARCHAR(50) NOT NULL,
    income_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    source          VARCHAR(20) NOT NULL DEFAULT 'manual',   -- manual | gmail | bank
    description     TEXT,
    confidence      NUMERIC(3, 2),
    deleted_at      TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

Note `message_id` is nullable with `ON DELETE SET NULL` (unlike `expenses.message_id`,
which cascades) — income may later come from Gmail/bank extraction with no originating
WhatsApp message (see `docs/design/open-challenges.md`). Phase 2's `income_agent.py` is
the only genuinely new work here; the table and its indexes are done.

### `conversation_turns` (already exists — Phase 0; Phase 4 consumes it)

```sql
CREATE TABLE IF NOT EXISTS conversation_turns (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL,          -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

Simpler than this document originally proposed (`conversation_messages`, with `intent` and
a `message_id` FK) — the shipped version is `user`/`assistant`/`content`/`created_at` only.
`handler.py` already calls `database.save_turn(user_id, role, content)` on both the inbound
message and every outbound reply (Phase 0, all branches — excel/report/expense alike), and
`database.load_recent_turns(user_id, max_turns, window_minutes)` already exists to read it
back (§7's `CONVERSATION_MAX_TURNS`/`CONVERSATION_WINDOW_MINUTES` config). Phase 4 is
wiring `load_recent_turns`'s output into `AgentRequest.conversation` and having an agent
actually use it — not building storage, which is done.

### `messages` already links to `users`

The optional later step this section originally proposed (`messages.customer_id`) is
already done, under the real name: `messages.user_id INTEGER NOT NULL REFERENCES
users(id)`, shipped in Phase 0. `expenses`/`incomes`/reporting queries already join on
`user_id`, not a raw phone string.

## 6. New-user onboarding chat (needs revisit before building)

**This section's premise no longer holds as written.** It assumed a `customers` table
with `onboarding_status`/`onboarding_step` columns gating product use until a welcome
script completes. What actually shipped in Phase 0 is `get_or_create_user(phone)`, which
auto-provisions a `users` row (id, phone, name, `prefs` JSONB, created_at) with **no
onboarding gate at all** — a brand-new phone number can log `"almuerzo 20000"` immediately
and it works, today, with default categories already seeded.

If a welcome/preference-collection chat is still wanted as a *courtesy* (not a gate), it
would need to be redesigned against the real schema before it's built:
- Progress tracking needs new nullable columns on `users` (e.g. `onboarding_completed_at`)
  rather than the invented `customers.onboarding_status`/`onboarding_step` — there's no
  `customers` table to add them to.
- "Category preferences" already has a home: `categories` (Phase 0) is a real per-user,
  per-`kind` table (`expense`/`income`), not a JSONB blob on the user row as originally
  proposed — an onboarding step to customize categories would insert/deactivate rows there
  via `get_user_categories`/`_seed_default_categories`-style functions, not write to
  `users.prefs`.
- The interruption-handling behavior this section describes (an expense message mid-chat
  short-circuits into the expense agent rather than blocking) is still the right instinct,
  but since there's no gate today, "interruption" isn't the right frame any more — it's
  just "the app already works with no onboarding," and a welcome chat would be additive
  UX layered on top, not something users can get stuck behind.

Not scheduled — flagging so a future planning pass starts from the real schema instead of
this section's original (now-inaccurate) premise.

## 7. Conversational memory (Phase 4)

**Storage already shipped in Phase 0** — this section originally proposed building it;
what's left is having an agent actually read it. Every inbound message and every outbound
reply is already recorded via `database.save_turn(user_id, role, content)`, called from
`handler.py` on all three branches (excel/report/expense) today:

```python
def save_turn(user_id: int, role: str, content: str) -> None: ...

def load_recent_turns(
    user_id: int,
    max_turns: int = CONVERSATION_MAX_TURNS,       # default 8
    window_minutes: int = CONVERSATION_WINDOW_MINUTES,  # default 30
) -> list[dict]: ...  # [{"role": ..., "content": ...}, ...], chronological
```

Simpler than what this section originally sketched — no `intent` column, no `message_id`
FK on the turn itself, and the hybrid cutoff is already implemented as
`CONVERSATION_MAX_TURNS`/`CONVERSATION_WINDOW_MINUTES` in `config.py` (8 turns / 30
minutes, not the 10 turns / 24 hours floated below — the shipped defaults, tune from
there). What Phase 4 actually adds: passing `load_recent_turns(user_id)`'s output into
`AgentRequest.conversation` (currently always `[]`) and having an agent use it — the
reporting agent, for follow-ups like "¿y la semana pasada?", is the concrete payoff this
section originally described.

### Context-window strategy: hybrid cutoff

Include the last **N=10 turns** *and* cap them to a **24-hour window**, whichever is
smaller. Two failure modes this avoids: a customer who messages in a rapid burst
shouldn't drag in an unbounded amount of history (the turn-count cap), and a customer who
messages once a week shouldn't get stale week-old context injected into an unrelated new
conversation (the time cap). Both bounds are deliberately conservative starting points —
tune against real usage once this phase ships.

### Cost/latency tradeoffs — explicit

- **The orchestrator's classification call does *not* receive conversation history.**
  Intent rarely depends on prior turns ("uber 14000" means the same thing whether or not
  the customer said something else five minutes ago), so the highest-frequency call in
  the whole system — the one that runs on every single message — stays cheap and
  context-free. History is only passed into the agent that actually gets dispatched to
  (expense/income/reporting), which already runs less often.
- **Every turn added to history linearly increases input tokens** on the calls that do
  carry it. This is the real cost of the feature — recommend watching
  `response.usage.input_tokens` after rollout rather than assuming the N=10 cap keeps cost
  flat; it doesn't, it just bounds the growth.
- **Prompt caching is unlikely to help much here.** Claude's prompt cache is a prefix
  match, and per-customer conversation history is the most volatile, latest-appended part
  of the prompt — it sits after the stable system prompt, which *is* a good caching
  candidate on its own (cache that separately), but the history tail itself won't
  consistently hit cache given how infrequently a single customer messages this app
  (personal expense tracking, not a chat product with rapid back-and-forth). Don't design
  around caching the history; design around bounding its size.

### What this unlocks

Once agents receive `conversation`, the reporting agent can interpret "¿y la semana
pasada?" as a follow-up to a previous report request instead of a standalone, ambiguous
message — this is the concrete product payoff of Phase 4, and the reason it's ordered
last: everything before it works fine without conversational context, but this phase adds
real behavioral capability on top.

## 8. Constraints preserved

Everything the user asked to keep unchanged, restated explicitly against this design:

- **Twilio ingestion is untouched.** Signature verification (`webhook.verify_signature`)
  and idempotency (`database.save_message`'s `ON CONFLICT (whatsapp_message_id) DO
  NOTHING`) both live entirely inside `handler.py`/`webhook.py`/`database.py`, all upstream
  of the orchestrator. Nothing in this proposal touches them.
- **The error contract extends unchanged.** `handler._handle_message`'s top-level
  `try/except` — unhandled error → `MSG_ERROR` Spanish reply → still return `200` so
  Twilio doesn't retry — now wraps a call to `orchestrator.handle_message(...)` the same
  way it wraps today's inline pipeline. **As shipped in Phase 1** (deviation 4, top of this
  document), agent-level failures are *not* caught into `AgentResponse(ok=False,
  error=...)` — they bubble to this same top-level `try/except`, exactly like every OpenAI
  call in `parser.py` today. The `ok=False`/`error` path this bullet originally described
  is still the eventual target (an agent explicitly reporting a handled failure, translated
  by the orchestrator into a friendlier reply than the generic `MSG_ERROR`) but nothing
  populates it yet.
- **Flat imports under `src/` are preserved.** Every new module in §4 is a flat file, not
  a subpackage — `deploy.sh` needs no changes for this rollout.

## 9. Phased rollout

Ordered so each phase ships something usable on its own, and later phases build on tables
and modules the earlier phases already introduced.

### Phase 1 — Contract + expense agent (on Claude) — **shipped, `feature/phase-1-expense-agent`**

- New: `claude_client.py`, `agent_types.py`, `expense_agent.py`, `orchestrator.py`.
- Scope narrower than originally drafted, per the deviations at the top of this document:
  text + audio-transcript only (image stays on OpenAI vision); no guardrail/classification
  call (single agent, nothing to route between yet); `handler.py` still performs the
  Twilio send; agent exceptions bubble rather than populate `AgentResponse(ok=False, ...)`.
- `handler.py` change: the text/audio expense sub-branch routes through
  `orchestrator.handle_message` instead of calling `parser.parse_expense` directly; excel,
  report, and image-expense branches are untouched.
- `evals/run.py` gained `_run_expense_agent()`, run against the same
  `evals/datasets/parse_expense.json` as `_run_parse_expense()` for direct comparison.
  **Not yet run against a live Anthropic key** — no `ANTHROPIC_API_KEY` was available in
  the environment this phase was implemented in. `THRESHOLDS["expense_agent"]` is still
  unset; running the live comparison and setting it is the one remaining step before this
  phase is fully verified (see §2's evals table).
- `classify_intent.json` (optional guardrail eval) not added — no guardrail exists yet.

### Phase 2 — Income agent

- `incomes` table already exists (Phase 0) — new work is `income_agent.py` only.
- Orchestrator gains real routing (expense vs. income vs. ...) — the first point where a
  classification call actually has something to decide between (see deviation 1).
- New: `evals/datasets/parse_income.json`, reusing `evals/scoring.py`'s `score_expense`.

### Phase 3 — Reporting agent + revisit onboarding

- `reporting_agent.py`: date-range parsing on Claude, replacing
  `parser.parse_report_request` for the report path; `reporting.format_report` stays
  deterministic Python (§2).
- Orchestrator's classifier expands to the full enum (expense/income/report/other) now
  that a guardrail has real off-topic cases to catch (report follow-ups outside the
  keyword pre-filter's exact/fuzzy match).
- Onboarding chat (§6) needs its design redone against the real `users`/`categories`
  schema before it's built — not scheduled as "the next thing," revisit when there's
  appetite for it.

### Phase 4 — Conversational memory (consumption)

- No new table — `conversation_turns`, `save_turn`, `load_recent_turns` already exist and
  are already being called on every message (Phase 0).
- `AgentRequest.conversation` (currently always `[]`) gets populated from
  `load_recent_turns(user_id)`.
- Reporting agent is the first to actually use it (follow-up report requests, e.g. "¿y la
  semana pasada?").

## 10. Open questions / risks

- **`THRESHOLDS["expense_agent"]` is unset — the one concrete blocker before Phase 1 is
  fully verified.** Run `uv run python -m evals.run` with a real `ANTHROPIC_API_KEY`,
  observe the Claude Haiku 4.5 numbers on `parse_expense.json`, and add the threshold entry
  (with headroom) before relying on `--check` for this path. Don't assume the
  OpenAI-tuned `parse_expense` thresholds (85%/95%/85%) transfer — a different model has a
  different accuracy profile.
- **Cost monitoring** becomes more important once Phase 4 ships, since conversation
  history is the one part of this design whose token cost grows with usage rather than
  staying flat per message. Recommend adding basic per-call token logging (via
  `response.usage`) before Phase 4, not after.
- **Onboarding (§6)** needs its design reworked against the real schema (`users`/
  `categories`, no `customers` table) before it's built — flagged as a design gap to close,
  not a decided behavior to revisit later, unlike the original draft's framing.
- **Image expense migration to Claude** (deviation 2) has no eval baseline to migrate
  against yet — `parse_expense_from_image` has zero existing test/eval coverage. Building
  that baseline is a prerequisite for migrating the image path, not just a nice-to-have.
