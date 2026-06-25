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

The entry point is [`handler.handler`](../src/handler.py). For a `POST` it runs
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

All application code lives in [`src/`](../src). Each module has a single responsibility:

| Module | Responsibility |
|--------|----------------|
| [`handler.py`](../src/handler.py) | Lambda entry point; orchestrates the full pipeline and intent routing |
| [`webhook.py`](../src/webhook.py) | Twilio signature verification and payload → message extraction |
| [`parser.py`](../src/parser.py) | OpenAI calls (text/vision/audio), intent detection, date-range extraction, confidence scoring |
| [`database.py`](../src/database.py) | PostgreSQL connection and all queries (`messages` + `expenses`) |
| [`media.py`](../src/media.py) | Download Twilio media and store it in S3 |
| [`excel.py`](../src/excel.py) | Build `.xlsx` workbooks and produce presigned S3 download URLs |
| [`reporting.py`](../src/reporting.py) | Format in-chat spending summaries grouped by currency and category |
| [`whatsapp.py`](../src/whatsapp.py) | Send messages, documents, and confirmations via the Twilio SDK |
| [`config.py`](../src/config.py) | Load all configuration from environment variables |

## Intent routing

A single inbound text is classified into exactly one of three intents, evaluated in this
order (first match wins):

1. **Excel export** — `parser.is_excel_request`: keyword match for `excel`, `xlsx`,
   `exportar`, `descargar`, "hoja de cálculo", etc.
2. **Report** — `parser.is_report_request`: a two-pass match (exact substring against a
   keyword/phrase list, then fuzzy single-word matching via `difflib` at cutoff `0.8`).
3. **Expense** — the default when neither export nor report keywords are present.

> Because Excel is checked first, a message that contains both an export verb and a
> report word (e.g. "exportame el resumen") is treated as an **Excel** request.

## External services

| Service | Used for | Auth |
|---------|----------|------|
| Twilio | Inbound webhook + outbound messages/documents + media download | Account SID + Auth Token; inbound requests verified by HMAC signature |
| OpenAI | Text parsing, receipt vision, audio transcription, report date ranges | API key |
| Neon (PostgreSQL) | Durable storage of messages and expenses | Connection string (`sslmode=require`) |
| AWS S3 | Inbound media archive + generated Excel files | Lambda execution role / default credential chain |

See [deployment.md](deployment.md) for how these are wired together.
