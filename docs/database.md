# Database Schema

Finance Tracker uses PostgreSQL (Neon in production). The schema is defined by
[`scripts/setup_db.sql`](../scripts/setup_db.sql).

As of **Phase 0** (multi-agent foundation), the schema is organized around a `users`
entity. Every row of every table is owned by a user, resolved from the verified WhatsApp
phone number. Some tables (`incomes`, `budgets`) are created now but only used by agents
introduced in later phases — see
[design/agentic-architecture.md](design/agentic-architecture.md).

```
users ──┬── messages ──── expenses (FK message_id, user_id)
        │             └── incomes  (FK message_id, user_id)
        ├── conversation_turns
        ├── categories
        └── budgets
```

## `users`

One row per person (auto-created on first message; identity comes from the verified
phone). Written by [`database.get_or_create_user`](../src/database.py).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | Referenced by every other table |
| `phone` | `VARCHAR(20) UNIQUE NOT NULL` | E.164, no `whatsapp:` prefix |
| `name` | `VARCHAR(100)` | Optional display name |
| `prefs` | `JSONB NOT NULL DEFAULT '{}'` | Free-form preferences (used by the Personal agent later) |
| `created_at` | `TIMESTAMPTZ` | |

## `messages`

One row per inbound WhatsApp message (raw audit log).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` `ON DELETE CASCADE` |
| `whatsapp_message_id` | `VARCHAR(128) UNIQUE NOT NULL` | Twilio `MessageSid`; basis for de-duplication |
| `phone_number` | `VARCHAR(20) NOT NULL` | Sender E.164 |
| `raw_text` | `TEXT NOT NULL` | Original body (may be empty for media-only) |
| `transcript` | `TEXT` | Whisper transcript for voice notes |
| `created_at` | `TIMESTAMPTZ` | |

## `conversation_turns`

Short-term chatbot memory: the recent back-and-forth used to resolve follow-ups like
*"cámbialo"* → *"¿a cuánto?"* → *"5000"*. Written by
[`database.save_turn`](../src/database.py), read by `load_recent_turns`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` `ON DELETE CASCADE` |
| `role` | `VARCHAR(16) NOT NULL` | `user` or `assistant` |
| `content` | `TEXT NOT NULL` | The message text |
| `created_at` | `TIMESTAMPTZ` | Used for the time-window memory query |

How much history is loaded is controlled by `CONVERSATION_MAX_TURNS` (cap) and
`CONVERSATION_WINDOW_MINUTES` (recency window) in [`config.py`](../src/config.py).

## `categories`

Per-user, customizable categories (categories are **data**, not a hardcoded list). Seeded
with the defaults from `config.py` when a user is created.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` `ON DELETE CASCADE` |
| `name` | `VARCHAR(50) NOT NULL` | e.g. `comida` |
| `kind` | `VARCHAR(16) NOT NULL DEFAULT 'expense'` | `expense` or `income` |
| `active` | `BOOLEAN NOT NULL DEFAULT TRUE` | Soft-disable instead of delete |
| | | `UNIQUE (user_id, name, kind)` |

The active expense categories are injected into the parser prompt per request by
[`database.get_user_categories`](../src/database.py).

## `expenses`

One row per parsed expense.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` |
| `message_id` | `INTEGER NOT NULL` | FK → `messages(id)` `ON DELETE CASCADE` |
| `amount` | `NUMERIC(12,2) NOT NULL` | |
| `currency` | `VARCHAR(10) NOT NULL DEFAULT 'COP'` | |
| `category` | `VARCHAR(50) NOT NULL` | |
| `expense_date` | `DATE NOT NULL DEFAULT CURRENT_DATE` | |
| `payment_method` | `VARCHAR(50)` | Nullable |
| `merchant` | `VARCHAR(100)` | Nullable |
| `description` | `TEXT` | |
| `confidence` | `NUMERIC(3,2)` | Completeness heuristic, see below |
| `source` | `VARCHAR(10) NOT NULL DEFAULT 'text'` | `text`, `audio`, or `image` |
| `deleted_at` | `TIMESTAMPTZ` | **Soft delete** — `NULL` means active; reads filter on this |
| `created_at` | `TIMESTAMPTZ` | |

### Confidence score

`confidence` is a completeness heuristic in
[`parser._estimate_confidence`](../src/parser.py), **not** a model probability:

| Condition | Points |
|-----------|--------|
| `amount` present and numeric | +0.5 |
| `category` present and not `otro` | +0.3 |
| `description` present | +0.2 |

## `incomes` (table created in Phase 0; agent in Phase 2)

Mirrors `expenses` for money coming in.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` |
| `message_id` | `INTEGER` | FK → `messages(id)` `ON DELETE SET NULL` |
| `amount` | `NUMERIC(12,2) NOT NULL` | |
| `currency` | `VARCHAR(10) NOT NULL DEFAULT 'COP'` | |
| `category` | `VARCHAR(50) NOT NULL` | |
| `income_date` | `DATE NOT NULL DEFAULT CURRENT_DATE` | |
| `source` | `VARCHAR(20) NOT NULL DEFAULT 'manual'` | `manual`, `gmail`, `bank` (future) |
| `description` | `TEXT` | |
| `confidence` | `NUMERIC(3,2)` | |
| `deleted_at` | `TIMESTAMPTZ` | Soft delete |
| `created_at` | `TIMESTAMPTZ` | |

## `budgets` (table created in Phase 0; agent in Phase 3)

Per-currency budgets, either global or scoped to a category.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` |
| `scope` | `VARCHAR(20) NOT NULL DEFAULT 'global'` | `global` or `category` |
| `category` | `VARCHAR(50)` | `NULL` when `scope = 'global'` |
| `currency` | `VARCHAR(10) NOT NULL DEFAULT 'COP'` | Budgets are **per-currency** |
| `period` | `VARCHAR(16) NOT NULL DEFAULT 'monthly'` | `monthly` or `weekly` |
| `amount` | `NUMERIC(12,2) NOT NULL` | The limit |
| `active` | `BOOLEAN NOT NULL DEFAULT TRUE` | |
| `created_at` | `TIMESTAMPTZ` | |

## Indexes

| Index | Column(s) |
|-------|-----------|
| `idx_messages_user` | `messages(user_id)` |
| `idx_turns_user_created` | `conversation_turns(user_id, created_at)` |
| `idx_categories_user` | `categories(user_id, kind)` |
| `idx_expenses_user` | `expenses(user_id)` |
| `idx_expenses_message_id` | `expenses(message_id)` |
| `idx_expenses_date` | `expenses(expense_date)` |
| `idx_incomes_user` | `incomes(user_id)` |
| `idx_incomes_date` | `incomes(income_date)` |
| `idx_budgets_user` | `budgets(user_id)` |

## Reset

Existing data is disposable in this stage. The top of
[`scripts/setup_db.sql`](../scripts/setup_db.sql) has a commented **RESET** block —
uncomment the `DROP TABLE … CASCADE` lines and run once to wipe, then run the `CREATE`
statements.

## Common query

Reports and Excel exports read through [`database.get_expenses`](../src/database.py),
now scoped by `user_id` and excluding soft-deleted rows:

```sql
SELECT amount, currency, category, expense_date,
       payment_method, merchant, description, source
FROM expenses
WHERE user_id = :user_id
  AND deleted_at IS NULL
  AND expense_date BETWEEN :min_date AND :max_date
ORDER BY expense_date, category;
```
