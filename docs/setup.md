# Local Setup

This guide gets the project running on your machine for development and testing. To ship
it to AWS, see [deployment.md](deployment.md).

## Prerequisites

- **Python 3.12** (pinned in `.python-version`)
- **[uv](https://docs.astral.sh/uv/)** package manager
- A **Neon** (or any PostgreSQL) database
- Accounts/credentials for **Twilio**, **OpenAI**, and **AWS** (S3)

## 1. Install dependencies

```bash
uv sync
```

This installs both runtime and dev dependencies from `pyproject.toml` into a local
virtual environment.

## 2. Configure environment variables

```bash
cp .env.example .env
# then edit .env and fill in real values
```

All configuration is read from environment variables at import time in
[`config.py`](../src/config.py). **All of the following are required** — the process will
fail to start if any is missing:

| Variable | Description | Example |
|----------|-------------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | `ACxxxxxxxx…` |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token (also used to download media and verify signatures) | `your_twilio_auth_token` |
| `TWILIO_WHATSAPP_NUMBER` | Sender number, with the `whatsapp:` prefix | `whatsapp:+14155238886` |
| `ALLOWED_PHONE_NUMBERS` | Comma-separated allowlist of senders (E.164, with `+`) | `+573001234567,+573009876543` |
| `OPENAI_API_KEY` | OpenAI API key | `sk-…` |
| `DATABASE_URL` | PostgreSQL connection string (`sslmode=require` for Neon) | `postgresql://user:pass@ep-xxx.neon.tech/db?sslmode=require` |
| `S3_BUCKET_NAME` | Bucket for inbound media and generated Excel files | `finance-tracker-dev` |
| `WEBHOOK_URL` | The exact public URL configured in Twilio; used for signature validation | `https://xxx.lambda-url.us-east-2.on.aws/` |

> **Why `WEBHOOK_URL` must match exactly:** Twilio computes the request signature over the
> full URL. If the value here differs from what Twilio calls (even a trailing slash),
> signature verification fails and every request returns `401`.

Non-secret tunables are hard-coded in [`config.py`](../src/config.py) and do not need to
be set as env vars:

| Constant | Value | Meaning |
|----------|-------|---------|
| `OPENAI_TEXT_MODEL` | `gpt-4o-mini` | Text parsing + report date extraction |
| `OPENAI_VISION_MODEL` | `gpt-4o` | Receipt/image parsing |
| `OPENAI_AUDIO_MODEL` | `whisper-1` | Voice-note transcription |
| `OPENAI_AUDIO_LANGUAGE` | `es` | Transcription language hint |
| `PRESIGNED_URL_EXPIRY_SECONDS` | `60` | Lifetime of Excel download links |
| `FUZZY_MATCH_CUTOFF` | `0.8` | Threshold for fuzzy report-keyword matching |

## 3. Create the database

Run the schema script against your database:

```bash
psql "$DATABASE_URL" -f scripts/setup_db.sql
```

Or paste the contents of [`scripts/setup_db.sql`](../scripts/setup_db.sql) into the
[Neon SQL Editor](https://console.neon.tech). The script is idempotent — it uses
`CREATE TABLE IF NOT EXISTS` and additive `ALTER TABLE … IF NOT EXISTS` migrations, so it
is safe to re-run. See [database.md](database.md) for the schema details.

## 4. Run the tests

The test suite uses `pytest` and mocks all external services (OpenAI, Twilio, the
database), so no live credentials are needed to run it.

```bash
uv run pytest tests/
```

> **Import path note:** the application modules live in `src/` and are imported by their
> bare names (e.g. `from parser import …`). If you see `ModuleNotFoundError`, run pytest
> with `src` on the path, e.g. `PYTHONPATH=src uv run pytest tests/`, or configure it in
> `pyproject.toml` / a `conftest.py`. See [development.md](development.md#running-the-tests).

## 5. Local end-to-end testing

[`notebooks/test_pipeline.ipynb`](../notebooks/test_pipeline.ipynb) exercises the full
parse → save → confirm pipeline without going through WhatsApp or Lambda. Use it to
iterate on parsing and reporting against your real database.
