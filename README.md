# Finance Tracker

A simple, frictionless personal finance tracking system that uses **WhatsApp as the primary interface**. Send a text message like `almuerzo 32000` and the system parses it with an LLM, stores structured data in PostgreSQL, and sends you a confirmation — all in seconds.

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
src/
├── handler.py      # Lambda entry point — routes POST messages
├── webhook.py      # Twilio signature verification + message extraction
├── parser.py       # OpenAI call + confidence scoring
├── database.py     # PostgreSQL connection + queries (messages + expenses tables)
├── whatsapp.py     # Send confirmation messages via Twilio SDK
└── config.py       # Environment variable loading
notebooks/
└── test_pipeline.ipynb  # Local testing without WhatsApp/Lambda
tests/
├── test_webhook.py
├── test_parser.py
└── test_database.py
scripts/
├── deploy.sh       # Package and deploy ZIP to Lambda (Linux x86_64 compatible)
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
| `OPENAI_API_KEY` | OpenAI API key |
| `DATABASE_URL` | Neon PostgreSQL connection string |

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
./scripts/deploy.sh
aws lambda update-function-code --function-name finance-tracker --zip-file fileb://lambda.zip
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
- **No secrets in code** — all credentials loaded from environment variables (upgrade to AWS SSM Parameter Store for production)

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
