# Finance Tracker

A simple, frictionless personal finance tracking system that uses **WhatsApp as the primary interface**. Send a text message like `almuerzo 32000` and the system parses it with an LLM, stores structured data in PostgreSQL, and sends you a confirmation — all in seconds.

## Documentation

Full documentation lives in [`docs/`](docs/README.md):

- [Architecture](docs/architecture.md) — components, request flow, and module responsibilities
- [Setup](docs/setup.md) — local development, environment variables, database, tests
- [User Guide](docs/usage.md) — logging expenses, reports, and Excel exports over WhatsApp
- [Database](docs/database.md) — schema, columns, indexes, and migrations
- [Deployment](docs/deployment.md) — AWS Lambda, S3, IAM, and Twilio configuration
- [Development](docs/development.md) — project layout, testing, conventions, known limitations

## How It Works

1. **You send a message** on WhatsApp (e.g., `uber 14000`, `mercado 120000 tarjeta`)
2. **Twilio receives it** and forwards the payload to a Lambda Function URL webhook
3. **Signature verification** ensures the request is genuinely from Twilio
4. **GPT-4o-mini** extracts structured data: amount, category, payment method, merchant
5. **Data is saved** to PostgreSQL (Neon) — raw message + structured expense + confidence score
6. **You get a confirmation** back on WhatsApp: `✅ Registré COP 14.000 en transporte para 2026-04-03`

## Tech Stack

| Component | Technology |
|-----------|------------|
| Channel | Twilio WhatsApp Sandbox → WhatsApp Cloud API (Meta) in production |
| Compute | AWS Lambda + Function URL |
| Language | Python 3.12 |
| Intelligence | OpenAI GPT-4o-mini |
| Database | Neon (serverless PostgreSQL) |
| Package manager | uv |

## Project Structure

```
src/finance_tracker/          # The application package (installed into the Lambda ZIP)
├── handler.py                # Lambda handler: webhook → pipeline → reply
├── config.py                 # Environment variables + tunable constants
├── core/                     # Agent contract (AgentRequest/AgentResponse), Claude client,
│                             #   confidence heuristic, text normalization
├── routing/                  # fast_paths.py (keyword checks) + orchestrator.py (dispatch)
├── agents/                   # expenses.py, incomes.py, reporting.py (+ report_format.py)
├── channels/whatsapp/        # webhook.py (Twilio verification) + sender.py (outbound)
├── storage/                  # database.py (Postgres), media.py (S3), excel.py (.xlsx)
├── legacy/                   # openai_parser.py: OpenAI vision/Whisper/Excel-date paths
└── observability/            # tracing.py (optional Phoenix)
lambda/handler.py             # Zip-root entry point → finance_tracker.handler.handler
tests/                        # pytest, mirrors the package (agents/, routing/, ...)
evals/                        # LLM evals: datasets/, scoring.py, run.py
notebooks/
└── test_pipeline.ipynb  # Local testing without WhatsApp/Lambda
scripts/
├── deploy.py       # Test, build, upload and smoke-test the Lambda (one command)
└── setup_db.sql    # Database schema creation
```

## Database Schema

```
messages                         expenses
────────────────────────────     ────────────────────────────
id (PK)                          id (PK)
whatsapp_message_id (unique)     message_id (FK → messages.id)
phone_number                     amount
raw_text                         category
created_at                       expense_date
                                 payment_method
                                 merchant
                                 description
                                 confidence
                                 created_at
```

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your credentials in .env
```

### 3. Environment Variables

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_WHATSAPP_NUMBER` | Twilio sandbox number (e.g. `whatsapp:+14155238886`) |
| `ALLOWED_PHONE_NUMBERS` | Comma-separated allowed senders (e.g. `+573001234567,+573009876543`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude router + agents) |
| `OPENAI_API_KEY` | OpenAI API key (Whisper, image expenses, Excel dates) |
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `S3_BUCKET_NAME` | Bucket for inbound media and Excel exports |
| `WEBHOOK_URL` | Exact webhook URL configured in Twilio (used to verify signatures) |

### 4. Create the database

Paste `scripts/setup_db.sql` into the [Neon SQL Editor](https://console.neon.tech) and run it, or:

```bash
psql "$DATABASE_URL" -f scripts/setup_db.sql
```

### 5. Run tests

```bash
uv run pytest tests/
```

### 6. Local testing

Open `notebooks/test_pipeline.ipynb` to test the full pipeline (parse → save → confirm) without WhatsApp or Lambda.

### 7. Deploy to Lambda

```bash
uv run python scripts/deploy.py   # see docs/deployment.md for the one-time AWS setup
```

Lambda configuration:
- **Handler:** `handler.handler`
- **Runtime:** Python 3.12
- **Architecture:** x86_64
- **Timeout:** 30 seconds
- **Function URL:** Enabled (Auth type: NONE)
- **Environment variables:** all 6 variables from `.env`

### 8. Configure Twilio webhook

In [Twilio Console](https://console.twilio.com) → Messaging → Try it out → Send a WhatsApp message → Sandbox Settings:

- **When a message comes in:** `https://your-lambda-url.lambda-url.us-east-1.on.aws/`
- **Method:** HTTP POST

## Security

- **Webhook signature verification** — every incoming POST is verified using Twilio's HMAC validator
- **Phone number allowlist** — only messages from `ALLOWED_PHONE_NUMBERS` are processed
- **No secrets in code or in the Lambda configuration** — locally from `.env`; in production from AWS SSM Parameter Store (SecureString), loaded once per cold start

## MVP Scope

**Included:**
- Expense registration via WhatsApp text messages
- Automatic LLM parsing (amount, category, payment method, merchant)
- Two-table PostgreSQL schema (messages + expenses) ready for multi-expense messages
- Instant WhatsApp confirmation
- Multiple allowed senders
- CloudWatch logging at every pipeline step
- Basic categories: comida, transporte, mercado, salud, entretenimiento, hogar, educacion, ropa, servicios, otro

**Not included (future):**
- Spending reports and summaries
- Dashboards
- Google Sheets export
- Voice or image messages
- Multiple expenses per message
- Budgets or alerts

## Roadmap

1. **MVP** ✅ — Validate the habit: "Will I consistently use WhatsApp to track expenses?"
2. **Reporting** — In-chat summaries (`resumen semana`) and PDF/Excel reports
3. **Data Export** — Google Sheets sync for manual analysis
4. **Production channel** — Migrate from Twilio sandbox to WhatsApp Cloud API (Meta) directly
5. **AWS hardening** — Move from Neon to RDS PostgreSQL + secrets to SSM Parameter Store
