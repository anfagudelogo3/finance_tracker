# Finance Tracker — Documentation

Finance Tracker is a frictionless personal-finance system that uses **WhatsApp as the
primary interface**. You send a message like `almuerzo 32000`, an LLM parses it into
structured data, it is stored in PostgreSQL, and you get an instant confirmation.

It also understands **voice notes** and **photos of receipts**, and can produce
**in-chat spending reports** and **Excel exports** on request.

## Documentation map

| Document | What it covers |
|----------|----------------|
| [architecture.md](architecture.md) | System components, request flow, and module responsibilities |
| [setup.md](setup.md) | Local development: dependencies, environment variables, database, tests |
| [usage.md](usage.md) | End-user guide: logging expenses, reports, Excel exports, supported commands |
| [database.md](database.md) | Full schema, columns, indexes, and migrations |
| [deployment.md](deployment.md) | Packaging and deploying to AWS Lambda, S3, and Twilio configuration |
| [development.md](development.md) | Project layout, testing strategy, conventions, and known limitations |
| [evaluation.md](evaluation.md) | LLM eval framework: programmatic scoring, Phoenix tracing, how to run |

### Design (where the project is going)

| Document | What it covers |
|----------|----------------|
| [agent-architecture.md](../agent-architecture.md) | **Source of truth.** Orchestrator/agent contract, Claude migration, phased rollout |
| [design/agentic-architecture.md](design/agentic-architecture.md) | Superseded past Phase 0 — kept for Phase-0 history (the `users`/`categories`/`conversation_turns` schema this shipped is still current) |
| [design/open-challenges.md](design/open-challenges.md) | Deferred capabilities: proactive scheduled alerts, Gmail/bank income |

## At a glance

```
WhatsApp ──> Twilio ──> Lambda Function URL ──> OpenAI / Claude ──> Neon (PostgreSQL)
   ▲                         │                                          │
   └─────── confirmation ────┴──── media stored in S3 ──────────────────┘
```

| Component | Technology |
|-----------|------------|
| Channel | Twilio WhatsApp Sandbox → WhatsApp Cloud API (Meta) in production |
| Compute | AWS Lambda + Function URL |
| Language | Python 3.12 |
| Parsing | Claude `claude-haiku-4-5-20251001` (expense text/audio-transcript); OpenAI `gpt-4o` (expense image, still migrating), `gpt-4o-mini` (report date-range parsing, still migrating), `whisper-1` (audio transcription, permanent) |
| Database | Neon (serverless PostgreSQL) |
| Media / exports | AWS S3 |
| Package manager | uv |

## Quick start

```bash
uv sync                       # install dependencies
cp .env.example .env          # then fill in credentials
psql "$DATABASE_URL" -f scripts/setup_db.sql   # create the schema
```

See [setup.md](setup.md) for the full walkthrough and [deployment.md](deployment.md) to
ship it.
