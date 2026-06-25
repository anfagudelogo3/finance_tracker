# Development Guide

Conventions, project layout, testing, and current behavioral caveats for anyone working
on the code.

## Project layout

```
finance_tracker/
├── src/                      # Application code (deployed flat into the Lambda ZIP)
│   ├── handler.py            # Lambda entry point + intent routing
│   ├── webhook.py            # Twilio signature verification + message extraction
│   ├── parser.py             # OpenAI calls, intent detection, confidence scoring
│   ├── database.py           # PostgreSQL connection + queries
│   ├── media.py              # Twilio media download → S3
│   ├── excel.py              # .xlsx generation + presigned URLs
│   ├── reporting.py          # In-chat report formatting
│   ├── whatsapp.py           # Outbound Twilio messages/documents
│   └── config.py             # Environment-variable configuration
├── tests/                    # pytest unit tests (external services mocked)
├── scripts/
│   ├── setup_db.sql          # Schema + idempotent migrations
│   └── deploy.sh             # Build the Lambda ZIP
├── notebooks/
│   └── test_pipeline.ipynb   # Manual end-to-end pipeline testing
├── docs/                     # You are here
├── pyproject.toml            # Dependencies (uv)
└── .env.example              # Template for required environment variables
```

> **Flat imports:** modules import each other by bare name (`from parser import …`,
> `from config import …`) because `deploy.sh` copies everything in `src/` to the root of
> the Lambda package, where they sit alongside `handler.py`. Keep imports flat — do not
> introduce a `src.` package prefix, or the Lambda runtime won't resolve them.

## Conventions

- **Single responsibility per module** — see the table in
  [architecture.md](architecture.md#modules). New external integrations get their own
  module.
- **Configuration through `config.py`** — read all env vars and tunable constants there,
  not scattered across modules.
- **Logging over printing** — every module uses the stdlib `logging` module; the handler
  logs each pipeline step so CloudWatch tells the full story of a request. Match that
  style for new code.
- **Fail friendly** — user-facing errors should produce a Spanish reply (see `MSG_ERROR`
  / `MSG_EMPTY_EXPENSE` in `config.py`), not an unhandled exception. The top-level
  `try/except` in `handler._handle_message` is the safety net.
- **Spanish for user-facing strings, English for code/comments.**

## Running the tests

```bash
uv run pytest tests/
```

The tests mock OpenAI, Twilio, and the database, so they need no live credentials.

Because the application modules live in `src/` and are imported by bare name, pytest must
have `src` on its import path. If you hit `ModuleNotFoundError`, run:

```bash
PYTHONPATH=src uv run pytest tests/
```

To make this automatic, add one of the following:

- in `pyproject.toml`:

  ```toml
  [tool.pytest.ini_options]
  pythonpath = ["src"]
  ```

- or a `tests/conftest.py` that inserts `src` into `sys.path` and sets placeholder
  environment variables **before** the modules are imported (recall that `config.py`
  reads required env vars at import time).

### Test coverage status

Current unit tests cover the text parser, signature verification, message extraction, and
the two core database writes. Areas that are **not yet covered** and are good first
contributions:

- audio and image parsing paths (`parse_expense_from_image`, `transcribe_audio`)
- intent detection (`is_excel_request`, `is_report_request`, `parse_report_request`)
- report formatting (`reporting.format_report`) and Excel generation (`excel.py`)
- the handler's routing logic end to end

When adding fields to the data model, update the relevant tests and the
[database.md](database.md) docs together.

## Known limitations and caveats

These describe **current behavior** to be aware of when extending the system — not a
promise that they are fixed.

- **Dates use the server clock (UTC).** `expense_date` and the "today" used for report
  ranges are derived from the Lambda's wall clock, which runs in UTC. For a UTC-5 user,
  expenses logged late at night can be attributed to the next calendar day. Convert to
  `America/Bogota` if precise local dates matter.
- **No webhook idempotency.** `whatsapp_message_id` is `UNIQUE`, so a Twilio retry of an
  already-processed message raises a unique-violation that surfaces as a generic error
  reply. Consider `INSERT … ON CONFLICT DO NOTHING` with a short-circuit.
- **Database connections are opened per query.** Each call to `get_connection` opens a new
  connection; under load on warm Lambda containers this can pressure the database
  connection limit. Consider reusing a connection per invocation or closing explicitly.
- **One media item per message.** Only the first attachment is parsed; additional
  attachments are stored to S3 but not analyzed.
- **No conversational state.** Each message is independent — there is no memory across
  messages.

## Dependencies

Runtime dependencies (`pyproject.toml`): `openai`, `psycopg2-binary`, `twilio`,
`requests`, `boto3`, `openpyxl`. Dev group: `pytest`, `python-dotenv`, `ipykernel`.
Add new dependencies with `uv add <pkg>` (and `uv add --dev <pkg>` for dev tools) so
`uv.lock` stays in sync.

## Roadmap

1. **MVP** ✅ — validate the habit of tracking via WhatsApp
2. **Reporting** ✅ — in-chat summaries and Excel exports
3. **Data export** — Google Sheets sync
4. **Production channel** — migrate from the Twilio Sandbox to the WhatsApp Cloud API
5. **AWS hardening** — RDS/Neon production tier + secrets in SSM Parameter Store
