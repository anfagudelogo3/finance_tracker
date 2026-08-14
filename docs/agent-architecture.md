# Agent Architecture Proposal

**Status:** design proposal — not yet implemented. This document describes the target
architecture for evolving Finance Tracker from a linear parser pipeline
(`handler.py` → keyword routing → `parser.py` OpenAI calls) into a hand-rolled
orchestrator + specialist-agent architecture on Claude models. Twilio ingestion, signature
verification, and the Lambda entry point are unchanged — this is entirely about what
happens after `handler._handle_message` has a verified, authorized, saved message in
hand. See [architecture.md](architecture.md) for the current pipeline this replaces.

## 1. Overview

An **orchestrator** classifies each inbound message, enforces a personal-finance
guardrail, identifies the customer, and dispatches to one of four specialist agents:
**onboarding**, **expense**, **income**, and **reporting**. Every agent implements the
same request/response contract and returns a fully-composed reply; only the orchestrator
talks to Twilio. Every LLM call in the new architecture runs on Claude, with one
deliberate, permanent exception: audio transcription stays on OpenAI Whisper, because
Claude has no speech-to-text endpoint.

```
                    ┌───────────────────────────── AWS Lambda ─────────────────────────────┐
                    │                                                                        │
 WhatsApp  ── POST ─┼─▶ handler.py (unchanged)                                               │
    ▲               │     ├─ verify Twilio signature                                         │
    │               │     ├─ allowlist check                                                 │
    │               │     ├─ save_message (idempotent) ──────────────────► Neon (PG)         │
    │               │     └─ store_all_media ───────────────────────────► S3                 │
    │               │              │                                                          │
    │               │              ▼                                                          │
    │               │        orchestrator.py                                                  │
    │               │              ├─ customers.get_or_create(phone) ────► Neon (customers)   │
    │               │              ├─ onboarding incomplete? ─────────────┐                    │
    │               │              │                                     ▼                    │
    │               │              │                          onboarding_agent (Claude Haiku)  │
    │               │              ├─ keyword pre-filter (excel / report — unchanged)          │
    │               │              ├─ Claude Haiku: guardrail + intent classify                │
    │               │              │        │                                                  │
    │               │              │        ├─ off-topic ──▶ deflection reply, no dispatch     │
    │               │              │        ├─ expense ────▶ expense_agent (Haiku / Sonnet)    │
    │               │              │        ├─ income  ────▶ income_agent (Haiku / Sonnet)     │
    │               │              │        └─ report   ────▶ reporting_agent (Haiku)          │
    │               │              │                                                            │
    │  reply        │              └─ send reply_text / reply_attachment ──► Twilio             │
    └───────────────┼──────────────────────────────────────────────────────────────────────────┘
                    └────────────────────────────────────────────────────────────────────────┘
```

Audio path: `transcribe_audio` (OpenAI Whisper) runs before the orchestrator sees the
message, exactly as today — its output is just transcript text, so from the
orchestrator's point of view an audio message and a text message are identical.

## 2. Model provider change: OpenAI → Claude

### Per-agent model tiers

| Agent / call | Model | Why |
|---|---|---|
| Orchestrator — guardrail + intent classification | `claude-haiku-4-5` | One short classification call per inbound message; needs to be cheap and fast since it runs on every message. Structured-output-capable, so the intent enum comes back as validated JSON, not parsed prose. |
| Onboarding agent — parse name / email / category preferences | `claude-haiku-4-5` | Short, low-stakes conversational turns — same tier as the classifier. |
| Expense agent — text extraction | `claude-haiku-4-5` | Mirrors today's `gpt-4o-mini` tier: narrow, high-volume, cost-sensitive structured extraction from short WhatsApp messages. The current eval suite shows 93% overall accuracy at this tier — no evidence a stronger model is needed for the text path. |
| Expense agent — receipt/photo extraction | `claude-sonnet-5` | Mirrors today's `gpt-4o` tier: image understanding + reasoning to read a receipt reliably needs more capability than the text path. Sonnet 5 also has high-resolution vision (2576px long edge), an upgrade over what the current vision model offered. |
| Income agent — text / photo | Same split as expense: `claude-haiku-4-5` (text), `claude-sonnet-5` (photo/deposit-slip) | Structurally identical extraction task to expense, just a different category enum. |
| Reporting agent — date-range parsing | `claude-haiku-4-5` | Same narrow, deterministic-ish JSON shape as today's `parse_report_request` (`{min_date, max_date}`). |
| Reporting agent — report *formatting* | No LLM call | `reporting.format_report`'s existing grouping/totals logic stays deterministic Python — it's not a language task, and keeping it out of the LLM avoids hallucinated numbers in a place where correctness matters most. |
| Audio transcription | OpenAI Whisper (`whisper-1`) — **unchanged** | Claude has no ASR endpoint. This is a permanent, intentional exception, not a temporary gap — see the callout below. |

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

```python
# New
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_CLASSIFIER_MODEL = "claude-haiku-4-5"   # orchestrator + onboarding
CLAUDE_EXTRACTION_MODEL = "claude-haiku-4-5"   # expense/income text
CLAUDE_VISION_MODEL = "claude-sonnet-5"        # expense/income photo
CLAUDE_REPORT_MODEL = "claude-haiku-4-5"       # date-range parsing

# Unchanged — audio only
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_AUDIO_MODEL = "whisper-1"
OPENAI_AUDIO_LANGUAGE = "es"

# Removed
# OPENAI_TEXT_MODEL, OPENAI_VISION_MODEL — no longer used once expense/income/report
# text and vision calls move to Claude.
```

`.env.example` gains `ANTHROPIC_API_KEY`; `OPENAI_API_KEY`'s comment changes to note it's
scoped to audio transcription only.

New module `src/claude_client.py` — a module-scope singleton, mirroring how `parser.py`
today does `client = OpenAI(api_key=OPENAI_API_KEY)`:

```python
import anthropic
from config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
```

**Structured outputs, not prompt-described JSON.** Today's `parser.py` describes the
expected JSON shape in the system prompt and calls `json.loads(content)` with no error
handling — a malformed response is an unhandled exception that surfaces as `MSG_ERROR`.
Claude Haiku 4.5 supports `output_config.format` (JSON-schema-constrained output) and
`client.messages.parse()`. The new agents should use one of these instead of prompt-only
JSON — it closes a real gap in the current implementation, not just a provider swap.

### Evals framework changes

| File | Change |
|---|---|
| `evals/__init__.py`, `evals/run.py` | Bootstrap a dummy/real `ANTHROPIC_API_KEY` the same way `OPENAI_API_KEY` is bootstrapped today. `OPENAI_API_KEY` stays required too (Whisper). |
| `pyproject.toml` (`evals` group) | Add `openinference-instrumentation-anthropic` alongside `openinference-instrumentation-openai` (the latter stays, for Whisper tracing). |
| `src/tracing.py` | `setup_tracing()` gains an `AnthropicInstrumentor().instrument()` call alongside the existing `OpenAIInstrumentor().instrument()`. |
| `evals/run.py` → `THRESHOLDS` | **Must be re-baselined**, not carried over as-is. A different model has a different accuracy profile; assuming the current 85%/95%/90% thresholds hold for Claude Haiku 4.5 would be guessing. Run the full eval suite once the expense agent lands and set thresholds from the observed numbers. |
| `evals/scoring.py` | `score_expense` is reusable as-is for income (Phase 2) — same shape (`amount`/`currency`/`category`/`payment_method`/`merchant`), only the category enum differs. |
| `evals/datasets/` | New `parse_income.json` (Phase 2). Optionally, a new `classify_intent.json` (Phase 1) exercising the orchestrator's guardrail + intent classifier directly — e.g. `"cuéntame un chiste"` → off-topic, `"almuerzo 30000"` → expense, `"me pagaron 2000000"` → income, `"resumen del mes"` → report. This is the one genuinely new evaluation surface (nothing like it exists today) and it's cheap insurance against silent guardrail regressions. |

## 3. Orchestrator ↔ agent contract

All four agents — onboarding, expense, income, reporting — implement the same function
signature:

```python
def handle(request: AgentRequest) -> AgentResponse: ...
```

### `AgentRequest`

```python
@dataclass
class AgentRequest:
    customer: Customer           # id, phone_number, name, category_preferences, ...
    text: str                    # message body, or Whisper transcript for audio
    media: list[MediaItem]       # populated for image messages; empty otherwise
    message_type: str            # "text" | "audio" | "image"
    now: str                     # ISO datetime, America/Bogota
    conversation: list[ConversationTurn]   # empty list until Phase 4
```

### `AgentResponse`

```python
@dataclass
class AgentResponse:
    ok: bool
    reply_text: str                        # Spanish, ready to send as-is
    reply_attachment: PresignedFile | None  # Excel export only; None otherwise
    data: dict | list[dict] | None          # persisted rows / profile fields, for logging
    error: str | None                       # machine-readable code, only when ok=False
```

The orchestrator sends `reply_text` (and `reply_attachment`, if present) via
`whatsapp.send_message` / `whatsapp.send_document` exactly as `handler.py` does today.
**Agents never import or call `whatsapp.py` directly** — this is the one hard rule that
makes "orchestrator composes and sends the final reply" true by construction rather than
by convention.

### Worked example

Input message: `"uber 14000"` from a returning, fully-onboarded customer.

```json
// AgentRequest passed to expense_agent.handle(...)
{
  "customer": {"id": 7, "phone_number": "+573001234567", "name": "Andrés",
               "category_preferences": {}, "default_currency": "COP"},
  "text": "uber 14000",
  "media": [],
  "message_type": "text",
  "now": "2026-08-13T20:10:00-05:00",
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
            "confidence": 0.8, "expense_id": 42}],
  "error": null
}
```

### Orchestrator responsibilities, against this contract

1. `customers.get_or_create(phone)` — identify the customer by phone number (Phase 3+).
2. **If the customer's onboarding isn't complete**, dispatch to `onboarding_agent`
   instead of steps 3–5 below (see §7).
3. Keyword pre-filter for Excel export / report requests — unchanged from today's
   `parser.is_excel_request` / `is_report_request`, still evaluated before any LLM call.
4. Otherwise, one Claude Haiku call does guardrail + intent classification together
   (single call, not two — see §2's rationale for keeping this call cheap):
   `{"on_topic": bool, "intent": "expense" | "income" | "report" | "other"}`.
5. `on_topic: false` (or `intent: "other"`) → orchestrator replies with a deflection
   message directly; no agent is dispatched.
6. Otherwise dispatch to the matching agent, send its `reply_text`/`reply_attachment`.
7. (Phase 4) Persist both the user's turn and the assistant's reply to
   `conversation_messages`.

Because the keyword pre-filter for Excel/report still runs before the LLM classifier,
export and report intents remain **free** (no API call) for the common case — only
messages that don't match those keywords, and aren't from a customer mid-onboarding, pay
for a classification call. This mirrors today's design taste of cheap-deterministic-first.

## 4. Module layout under `src/`

Modules stay **flat** — no `src/agents/` subpackage. `scripts/deploy.sh` currently runs
`cp src/*.py build/`, which is non-recursive; a subpackage would need that script updated
before any of this could deploy. Staying flat means this proposal needs zero changes to
`deploy.sh`, which is a real advantage, not just constraint-following.

| Module | Responsibility |
|---|---|
| `claude_client.py` | Module-scope Anthropic client singleton, mirrors today's OpenAI client in `parser.py` |
| `agent_types.py` | Shared `AgentRequest` / `AgentResponse` dataclasses — the contract in §3 |
| `orchestrator.py` | Customer lookup, onboarding gate, keyword pre-filter, guardrail + intent classification, dispatch, reply composition |
| `expense_agent.py` | Identify, extract, classify, and persist an expense record (text + photo) |
| `income_agent.py` | Identify, extract, classify, and persist an income record (text + photo) |
| `reporting_agent.py` | Date-range parsing (Claude) + report formatting (deterministic, delegates to `reporting.py`) |
| `onboarding_agent.py` | New-customer welcome chat — name / email / category-preference collection (Phase 3) |
| `customers.py` | `customers` table queries — `get_or_create`, preference reads/writes (Phase 3) |
| `conversation.py` | `conversation_messages` table queries, context-window selection (Phase 4) |

`parser.py`, `database.py`, `reporting.py`, `excel.py`, `whatsapp.py`, `media.py`,
`webhook.py`, `handler.py` keep their current single responsibilities; `handler.py`'s only
change across this whole rollout is which function it calls after `store_all_media` —
the new-agent orchestrator instead of the current inline routing logic.

## 5. Schema changes

Written as additions to `scripts/setup_db.sql`, following its existing convention:
`CREATE TABLE IF NOT EXISTS` for new tables, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
migrations at the bottom for existing tables. All idempotent, safe to re-run.

### `customers` (Phase 3)

```sql
CREATE TABLE IF NOT EXISTS customers (
    id                      SERIAL PRIMARY KEY,
    phone_number            VARCHAR(20) UNIQUE NOT NULL,
    name                    VARCHAR(100),
    email                   VARCHAR(255),
    category_preferences    JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    timezone                VARCHAR(50) NOT NULL DEFAULT 'America/Bogota',
    onboarding_status       VARCHAR(20) NOT NULL DEFAULT 'pending',
    onboarding_step         SMALLINT NOT NULL DEFAULT 0,
    onboarding_completed_at TIMESTAMP WITH TIME ZONE,
    created_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers (phone_number);
```

`onboarding_status` is one of `pending` / `in_progress` / `completed` / `skipped`.
`category_preferences` is intentionally schemaless JSONB — it's meant to hold custom
category lists/aliases beyond the fixed `comida, transporte, mercado, ...` set (see §7),
and different customers will want different shapes of preference without a schema
migration each time.

### `income` (Phase 2)

Mirrors `expenses`' shape rather than overloading it with a `type` discriminator column —
income categories (`salario`, `freelance`, `inversion`, `regalo`, `otro`) are a different
enum from expense categories, and keeping them as separate tables keeps `category` meaning
one thing per table instead of needing a lookup table or conditional validation.

```sql
CREATE TABLE IF NOT EXISTS income (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category        VARCHAR(50) NOT NULL,
    income_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    source          VARCHAR(100),
    description     TEXT,
    confidence      NUMERIC(3, 2),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_income_message_id ON income (message_id);
CREATE INDEX IF NOT EXISTS idx_income_date ON income (income_date);
CREATE INDEX IF NOT EXISTS idx_income_category ON income (category);
```

### `conversation_messages` (Phase 4)

```sql
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    role            VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    intent          VARCHAR(20),
    message_id      INTEGER REFERENCES messages(id),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversation_customer_time
    ON conversation_messages (customer_id, created_at DESC);
```

`intent` is nullable (assistant rows don't have one); `message_id` is nullable (assistant
replies aren't a row in `messages`, which only stores inbound WhatsApp messages today).

### Optional, later: linking `messages` to `customers`

`messages.phone_number` already works for everything in this proposal — no phase
*requires* touching the `messages` table. As a later nice-to-have, once `customers` exists,
an additive nullable `messages.customer_id INTEGER REFERENCES customers(id)` (backfilled
via a phone-number join) would let `expenses`/`income`/reporting queries join on an ID
instead of a raw phone string. Not required for any phase above.

## 6. New-customer onboarding chat (Phase 3)

The first time a phone number messages the app, the app should have a short conversation
— name, email, income/expense category preferences — before the customer starts logging
expenses/income in earnest, rather than silently treating an unknown customer identically
to an established one.

### Trigger

`customers.get_or_create(phone)` either creates a new row (`onboarding_status='pending'`)
or finds an existing one that isn't `completed`/`skipped`. Either way, the orchestrator
routes to `onboarding_agent.handle(request)` **instead of** the keyword-filter →
classify → dispatch flow in §3.

### The script

Onboarding is a short, fixed, linear sequence — it doesn't need the general-purpose
`conversation_messages` table (that's Phase 4). Progress is tracked entirely in the two
new `customers` columns, so onboarding ships independently and doesn't block on
conversational memory landing first.

| Step | `onboarding_step` transition | Orchestrator says | Accepts |
|---|---|---|---|
| 0 | 0 → 1 | Welcome message; asks for name | any text |
| 1 | 1 → 2 | Acknowledges name; asks for email, explicitly optional ("escribe 'saltar' si prefieres no darlo") | any text, or "saltar" |
| 2 | 2 → 3 | Asks: standard categories, or custom ones? | free text — parsed by a Haiku call into `category_preferences` |
| 3 | 3 → `completed` | Confirmation message; sets `onboarding_completed_at = NOW()` | — |

Every step accepts `"saltar"` / `"skip"` — it advances `onboarding_step` and leaves that
field unset rather than blocking. Onboarding is a courtesy, never a gate on using the
product.

### Interruption handling

If a message arriving mid-onboarding strongly matches an expense/income shape (an amount
plus a recognizable keyword — the same deterministic signal the existing
`is_excel_request`/`is_report_request`-style keyword checks already use), the orchestrator
short-circuits: it processes *that one message* through the expense/income agent,
leaves `onboarding_status`/`onboarding_step` untouched, and re-asks the current onboarding
question on the customer's next reply. This is a deliberate default — a new user who opens
the app by immediately logging `"almuerzo 20000"` should get that expense recorded, not be
blocked behind a name prompt. It's documented here as a decided product behavior, not an
open question, though it's worth revisiting once there's real usage data on how often it
actually triggers.

### Model

`claude-haiku-4-5` for every parsing step (name extraction, loose email validation,
category-preference parsing from free text) — same tier as the orchestrator's classifier,
consistent with "cheap model for short, low-stakes conversational turns."

## 7. Conversational memory (Phase 4)

Today, every Twilio message is handled as an independent request — no memory across
messages. This section proposes evolving toward chatbot-style behavior.

### Storage

`conversation_messages` (§5) + `src/conversation.py`:

```python
def save_turn(customer_id: int, role: str, content: str,
              intent: str | None, message_id: int | None) -> None: ...

def get_recent_context(customer_id: int, max_turns: int = 10,
                        max_age_hours: int = 24) -> list[ConversationTurn]: ...
```

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
  way it wraps today's inline pipeline. No new failure mode introduced by this
  architecture should surface as an unhandled exception; agent-level failures should
  return `AgentResponse(ok=False, error=...)` and let the orchestrator translate that into
  a friendly reply, with the top-level `try/except` remaining the last-resort safety net.
- **Flat imports under `src/` are preserved.** Every new module in §4 is a flat file, not
  a subpackage — `deploy.sh` needs no changes for this rollout.

## 9. Phased rollout

Ordered so each phase ships something usable on its own, and later phases build on tables
and modules the earlier phases already introduced.

### Phase 1 — Contract + expense agent (on Claude)

- New: `claude_client.py`, `agent_types.py`, `expense_agent.py`.
- Minimal `orchestrator.py`: keyword pre-filter unchanged; binary expense/other
  classification + guardrail (income and customers don't exist yet, so anything that
  isn't excel/report/expense/off-topic just isn't handled yet — acceptable since this
  phase is about proving the new path end-to-end, not full parity).
- `handler.py` change: route non-excel/report messages through the new orchestrator
  instead of calling `parser.parse_expense` directly.
- `evals/datasets/parse_expense.json` scoring migrates to exercise the new agent path
  (not the raw parser function); run the full suite and set `THRESHOLDS` from observed
  Claude Haiku 4.5 accuracy rather than carrying over the OpenAI-tuned numbers.
- Optional: `evals/datasets/classify_intent.json` to pin down guardrail behavior early.

### Phase 2 — Income agent

- New: `income` table, `income_agent.py`.
- Orchestrator's classifier expands to the full 4-way enum (expense/income/report/other).
- New: `evals/datasets/parse_income.json`, reusing `evals/scoring.py`'s `score_expense`.

### Phase 3 — Customers table + onboarding chat

- New: `customers` table (including `onboarding_status`/`onboarding_step`), `customers.py`,
  `onboarding_agent.py`.
- Orchestrator calls `customers.get_or_create` on every request and routes to onboarding
  per §6 when incomplete.
- Expense/income agents start reading `category_preferences`/`default_currency` from the
  customer row.
- `customer.name`, once set, can personalize confirmation replies (small UX win, optional).

### Phase 4 — Conversational memory

- New: `conversation_messages` table, `conversation.py`.
- Orchestrator persists both the user's turn and the assistant's reply after every
  message.
- Agents receive a populated `conversation` field per the §7 strategy; reporting agent is
  the first to actually use it (follow-up report requests).

## 10. Open questions / risks

- **Accuracy re-baselining is mandatory after Phase 1**, not optional cleanup — a
  different model has a different accuracy profile than the OpenAI models the current
  `THRESHOLDS` were tuned against. Treat the first full eval run against Claude Haiku 4.5
  as the actual baseline-setting exercise, not a sanity check against existing numbers.
- **Cost monitoring** becomes more important once Phase 4 ships, since conversation
  history is the one part of this design whose token cost grows with usage rather than
  staying flat per message. Recommend adding basic per-call token logging (via
  `response.usage`) before Phase 4, not after.
- **Onboarding interruption UX** (§6) is a decided default, not an open question, but it's
  the one behavior in this proposal built on judgment rather than a hard constraint —
  worth revisiting once there's real data on how often new customers try to log an
  expense before finishing onboarding.
