# Architecture

## Overview

Finance Tracker is a single AWS Lambda function fronted by a [Function URL][furl]. Twilio
delivers every inbound WhatsApp message to that URL as an HTTP `POST` with a URL-encoded
form body. The function verifies the request, parses the message (text, audio, or image)
with OpenAI, persists structured data to PostgreSQL, and replies through the Twilio API.

[furl]: https://docs.aws.amazon.com/lambda/latest/dg/lambda-urls.html

```
                        ┌──────────────────────── AWS ────────────────────────┐
                        │                                                      │
 WhatsApp user          │   Lambda Function URL (auth: NONE)                   │
      │                 │            │                                         │
      │  message        │            ▼                                         │
      ▼                 │      handler.handler                                 │
   Twilio  ── POST ─────┼──>  ├─ verify Twilio signature                       │
      ▲                 │     ├─ allowlist check                              │
      │                 │     ├─ save raw message ──────────────► Neon (PG)    │
      │                 │     ├─ store media ───────────────────► S3           │
      │  reply          │     ├─ route by intent / message type               │
      └─────────────────┼─────┤    ├─ OpenAI: parse / vision / whisper         │
                        │     │    └─ save expenses ────────────► Neon (PG)    │
                        │     └─ send confirmation / report / file ─► Twilio   │
                        └──────────────────────────────────────────────────────┘
```

## Request lifecycle

The entry point is [`handler.handler`](../src/finance_tracker/handler.py). For a `POST` it runs
`_handle_message`, which performs these steps in order:

1. **Parse the form body** — `_parse_form_body` decodes the (optionally base64-encoded)
   URL-encoded body into a flat dict of Twilio parameters.
2. **Verify the signature** — `webhook.verify_signature` validates Twilio's
   `X-Twilio-Signature` HMAC against the fixed `WEBHOOK_URL`. Invalid → `401`.
3. **Extract the message** — `webhook.extract_message` normalizes the sender, body, and
   any media into a structured dict. Non-message callbacks (e.g. delivery status) →
   `200` with an empty body.
4. **Authorize the sender** — the phone number must be in `ALLOWED_PHONE_NUMBERS`,
   otherwise the message is silently ignored (`200`).
5. **Persist the raw message** — `database.save_message` inserts a row into `messages`
   and returns its id.
6. **Store media** — if the message has attachments, `media.store_all_media` downloads
   each from Twilio and uploads it to S3, keeping the bytes in memory for the LLM.
7. **Route by intent**, in priority order:
   - **Excel export** if the text matches `is_excel_request` → build an `.xlsx`, upload
     to S3, send a presigned download link.
   - **Report** if the text matches `is_report_request` → resolve a date range, query
     expenses, format an in-chat summary.
   - **Expense** otherwise → parse by message type (audio → Whisper + text model,
     image → vision model, text → text model), save each expense, send a confirmation.
8. **Error handling** — the entire body is wrapped in a `try/except`. Any unhandled error
   is logged and the user receives a friendly Spanish error message; the function still
   returns `200` so Twilio does not retry indefinitely.

## Modules

All application code lives in the [`finance_tracker`](../src/finance_tracker) package. Each module has a single responsibility (paths relative to `src/finance_tracker/`):

| Module | Responsibility |
|--------|----------------|
| [`handler.py`](../src/finance_tracker/handler.py) | Lambda handler; runs the pipeline and picks the branch (Excel / report / image / text-audio) |
| [`config.py`](../src/finance_tracker/config.py) | Load all configuration from environment variables (filled from SSM Parameter Store at cold start in the Lambda, via `core/ssm_config.py`); tunable constants and reply strings |
| [`core/agent_types.py`](../src/finance_tracker/core/agent_types.py) | `AgentRequest` / `AgentResponse`, the contract every agent implements (incl. the user `profile`) |
| [`core/agent_loop.py`](../src/finance_tracker/core/agent_loop.py) | Claude tool-use loop: runs until `end_turn`; max iterations / timeout as safeguards |
| [`core/claude_client.py`](../src/finance_tracker/core/claude_client.py) | Anthropic client singleton |
| [`core/confidence.py`](../src/finance_tracker/core/confidence.py), [`core/text.py`](../src/finance_tracker/core/text.py) | Confidence heuristic; accent-insensitive text normalization |
| [`routing/fast_paths.py`](../src/finance_tracker/routing/fast_paths.py) | All deterministic keyword checks (Excel, report, income) and `fast_route`, the narrowed free path in front of the router |
| [`routing/orchestrator.py`](../src/finance_tracker/routing/orchestrator.py) | Hybrid dispatch: `fast_route` when unambiguous, otherwise the router |
| [`routing/router_agent.py`](../src/finance_tracker/routing/router_agent.py) | Claude router: delegates to agents as tools, asks for clarification, or declines; assembles the reply |
| [`agents/expenses.py`](../src/finance_tracker/agents/expenses.py), [`agents/incomes.py`](../src/finance_tracker/agents/incomes.py) | Claude extraction + persistence of expenses / incomes |
| [`agents/reporting.py`](../src/finance_tracker/agents/reporting.py) | Date-range resolution (deterministic + Claude fallback) and in-chat reports |
| [`agents/report_format.py`](../src/finance_tracker/agents/report_format.py) | Format in-chat spending summaries grouped by currency and category |
| [`channels/whatsapp/webhook.py`](../src/finance_tracker/channels/whatsapp/webhook.py) | Twilio signature verification and payload → message extraction |
| [`channels/whatsapp/sender.py`](../src/finance_tracker/channels/whatsapp/sender.py) | Send messages, documents, and confirmations via the Twilio SDK |
| [`storage/database.py`](../src/finance_tracker/storage/database.py) | PostgreSQL connection and all queries |
| [`storage/media.py`](../src/finance_tracker/storage/media.py) | Download Twilio media and store it in S3 |
| [`storage/excel.py`](../src/finance_tracker/storage/excel.py) | Build `.xlsx` workbooks and produce presigned S3 download URLs |
| [`legacy/openai_parser.py`](../src/finance_tracker/legacy/openai_parser.py) | OpenAI paths not yet migrated: image vision, Excel date ranges, original text parser; Whisper transcription (permanent) |
| [`observability/tracing.py`](../src/finance_tracker/observability/tracing.py) | Optional Phoenix/OpenTelemetry tracing |

## Intent routing

A single inbound text is classified into exactly one of three intents, evaluated in this
order (first match wins):

1. **Excel export** — `fast_paths.is_excel_request`: keyword match for `excel`, `xlsx`,
   `exportar`, `descargar`, "hoja de cálculo", etc.
2. **Report** — `fast_paths.is_report_request`: a two-pass match (exact substring against a
   keyword/phrase list, then fuzzy single-word matching via `difflib` at cutoff `0.8`).
3. **Expense** — the default when neither export nor report keywords are present.

> Because Excel is checked first, a message that contains both an export verb and a
> report word (e.g. "exportame el resumen") is treated as an **Excel** request.

**Expense dispatch, since [agent-architecture.md](agent-architecture.md) Phase 1:** the
three keyword-routed branches above are unchanged, but the expense branch now splits by
message type. Text and audio-transcript messages build an `AgentRequest` and go through
`orchestrator.handle_message` → `expense_agent.handle` (Claude, forced tool-use
extraction). Image messages are unchanged — still `legacy.openai_parser.parse_expense_from_image`
(OpenAI vision) called directly from `handler.py`. See `agent-architecture.md` for why the
split is scoped this way and what's still planned.

## External services

| Service | Used for | Auth |
|---------|----------|------|
| Twilio | Inbound webhook + outbound messages/documents + media download | Account SID + Auth Token; inbound requests verified by HMAC signature |
| OpenAI | Text parsing, receipt vision, audio transcription, report date ranges | API key |
| Neon (PostgreSQL) | Durable storage of messages and expenses | Connection string (`sslmode=require`) |
| AWS S3 | Inbound media archive + generated Excel files | Lambda execution role / default credential chain |

See [deployment.md](deployment.md) for how these are wired together.
