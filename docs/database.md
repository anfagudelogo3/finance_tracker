# Database Schema

Finance Tracker uses PostgreSQL (Neon in production). The schema is defined and migrated
by [`scripts/setup_db.sql`](../scripts/setup_db.sql), which is idempotent and safe to
re-run.

Two tables model the data: every inbound WhatsApp message is stored once in `messages`,
and the structured expenses extracted from it are stored in `expenses` (one message can
yield several expenses).

```
messages                              expenses
─────────────────────────────        ─────────────────────────────
id              PK                    id              PK
whatsapp_message_id  UNIQUE           message_id      FK → messages.id
phone_number                          amount
raw_text                              currency
transcript                            category
created_at                            expense_date
                                      payment_method
                                      merchant
                                      description
                                      confidence
                                      source
                                      created_at
```

## `messages`

One row per inbound WhatsApp message (the raw event), written by
[`database.save_message`](../src/database.py).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | Internal id, referenced by `expenses.message_id` |
| `whatsapp_message_id` | `VARCHAR(128)` `UNIQUE NOT NULL` | Twilio `MessageSid`; the uniqueness constraint is the basis for de-duplication |
| `phone_number` | `VARCHAR(20) NOT NULL` | Sender in E.164, without the `whatsapp:` prefix |
| `raw_text` | `TEXT NOT NULL` | Original message body (may be empty for media-only messages) |
| `transcript` | `TEXT` | Whisper transcript for voice notes; `NULL` otherwise. Set by `update_message_transcript` |
| `created_at` | `TIMESTAMPTZ` | Defaults to `NOW()` |

## `expenses`

One row per parsed expense, linked to its source message, written by
[`database.save_expense`](../src/database.py).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `message_id` | `INTEGER NOT NULL` | FK → `messages(id)` `ON DELETE CASCADE` |
| `amount` | `NUMERIC(12,2) NOT NULL` | No currency symbol |
| `currency` | `VARCHAR(10) NOT NULL DEFAULT 'COP'` | ISO-like code (`COP`, `USD`, …) |
| `category` | `VARCHAR(50) NOT NULL` | One of the fixed categories (see [usage.md](usage.md)) |
| `expense_date` | `DATE NOT NULL DEFAULT CURRENT_DATE` | The day the expense is attributed to |
| `payment_method` | `VARCHAR(50)` | Nullable |
| `merchant` | `VARCHAR(100)` | Nullable |
| `description` | `TEXT` | Short summary |
| `confidence` | `NUMERIC(3,2)` | Heuristic score in `[0, 1]` (see below) |
| `source` | `VARCHAR(10) NOT NULL DEFAULT 'text'` | How the expense was captured: `text`, `audio`, or `image` |
| `created_at` | `TIMESTAMPTZ` | Defaults to `NOW()` |

### Confidence score

`confidence` is a simple completeness heuristic computed in
[`parser._estimate_confidence`](../src/parser.py), **not** a model probability:

| Condition | Points |
|-----------|--------|
| `amount` present and numeric | +0.5 |
| `category` present and not `otro` | +0.3 |
| `description` present | +0.2 |

So a fully populated expense scores `1.0`; an `otro`-category expense with an amount and
description scores `0.7`.

## Indexes

Created by the schema script for the common access patterns (per-user date-range
queries and category grouping):

| Index | Column(s) |
|-------|-----------|
| `idx_expenses_message_id` | `expenses(message_id)` |
| `idx_expenses_date` | `expenses(expense_date)` |
| `idx_expenses_category` | `expenses(category)` |
| `idx_messages_phone` | `messages(phone_number)` |

## Common query

Reports and Excel exports read through [`database.get_expenses`](../src/database.py),
which joins the two tables and filters by sender and inclusive date range:

```sql
SELECT e.amount, e.currency, e.category, e.expense_date,
       e.payment_method, e.merchant, e.description, e.source
FROM expenses e
JOIN messages m ON e.message_id = m.id
WHERE m.phone_number = :phone_number
  AND e.expense_date BETWEEN :min_date AND :max_date
ORDER BY e.expense_date, e.category;
```

## Migrations

The bottom of [`scripts/setup_db.sql`](../scripts/setup_db.sql) contains additive
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` statements (`currency`, `source`, `transcript`)
so that existing deployments can be upgraded by simply re-running the script. When adding
a new column, follow the same pattern rather than editing the `CREATE TABLE` in place.
