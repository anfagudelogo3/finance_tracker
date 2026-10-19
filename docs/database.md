# Database Schema

Finance Tracker uses PostgreSQL (Neon in production). The schema is defined by
[`scripts/setup_db.sql`](../scripts/setup_db.sql).

As of **Phase 0** (multi-agent foundation), the schema is organized around a `users`
entity. Every row of every table is owned by a user, resolved from the verified WhatsApp
phone number. Some tables (`incomes`, `budgets`) are created now but only used by agents
introduced in later phases — see [agent-architecture.md](agent-architecture.md) for the
current agent rollout plan (identity/categories/conversation-turn design here is unchanged
by it; only the agent architecture built on top of this schema changed).

```
users ──┬── messages ──── expenses (FK message_id, user_id, category_id)
        │             └── incomes  (FK message_id, user_id, category_id)
        ├── user_prefs (append-only, latest row wins)
        ├── conversation_turns
        ├── categories ── category_events (append-only history)
        └── budgets
```

## `users`

One row per person (auto-created on first message; identity comes from the verified
phone). Written by [`database.get_or_create_user`](../src/finance_tracker/storage/database.py).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | Referenced by every other table |
| `phone` | `VARCHAR(20) UNIQUE NOT NULL` | E.164, no `whatsapp:` prefix |
| `name` | `VARCHAR(100)` | Optional display name |
| `prefs` | `JSONB NOT NULL DEFAULT '{}'` | **Deprecated, not read.** Preferences live in [`user_prefs`](#user_prefs-append-only-preference-history) |
| `created_at` | `TIMESTAMPTZ` | |

## `user_prefs` (append-only preference history)

Every preference change **inserts a new row** with the full preferences snapshot; rows are
never updated or deleted. A user's current preferences are their **latest row**
(`ORDER BY created_at DESC, id DESC LIMIT 1`), read by
[`database.get_user_prefs`](../src/finance_tracker/storage/database.py) and written only by
`database.append_user_prefs` (which merges the change onto the latest snapshot inside a
transaction that locks the user row). No row means no preferences set: the code treats the
user as `{"profile": {"type": "personal"}}`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | Tie-breaker when two rows share `created_at` |
| `user_id` | `INTEGER NOT NULL` FK → `users(id)` | `ON DELETE CASCADE` |
| `prefs` | `JSONB NOT NULL` | Full snapshot after the change |
| `changes` | `JSONB` | Only the keys that changed in this row |
| `source` | `VARCHAR(20) NOT NULL DEFAULT 'manual'` | `onboarding` \| `onboarding_restart` \| `users_agent` \| `manual` |
| `created_at` | `TIMESTAMPTZ` | When the change was made |

Keys in `prefs` (see [agent-roadmap.md](agent-roadmap.md) §5.1):

```jsonc
{
  "profile": {"type": "personal"},                    // or {"type": "business", "business": {"name", "industry", "description"}}
  "default_currency": "COP",                          // Phase 6
  "report": {"default_period": "month", "group_by": "category"},  // Phase 6
  "onboarding": {"step": "done", "completed_at": "..."}            // Phase 6
}
```

Setting a profile by hand until the Phase 6 users agent exists:

```sql
-- First preferences row for user 7: snapshot and changes are the same object
WITH p AS (SELECT '{"profile": {"type": "business", "business": {"industry": "restaurante",
  "description": "Vende almuerzos. Compra insumos, paga nómina y arriendo."}}}'::jsonb AS v)
INSERT INTO user_prefs (user_id, prefs, changes, source)
SELECT 7, v, v, 'manual' FROM p;
```

(For a user who already has preferences, merge onto the latest snapshot instead, or use
`database.append_user_prefs`.)

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
[`database.save_turn`](../src/finance_tracker/storage/database.py), read by `load_recent_turns`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` `ON DELETE CASCADE` |
| `role` | `VARCHAR(16) NOT NULL` | `user` or `assistant` |
| `content` | `TEXT NOT NULL` | The message text |
| `created_at` | `TIMESTAMPTZ` | Used for the time-window memory query |

How much history is loaded is controlled by `CONVERSATION_MAX_TURNS` (cap) and
`CONVERSATION_WINDOW_MINUTES` (recency window) in [`config.py`](../src/finance_tracker/config.py).

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

The active expense categories are injected into the extraction prompts per request by
[`database.get_user_categories`](../src/finance_tracker/storage/database.py).

`expenses` and `incomes` reference a category by **`category_id`**, never by name (Phase 6a), so
renaming a category is a single-row change and past records show the new name. The agents still
pick a category by name; `save_expense` / `save_income` resolve it to the user's id
(case-insensitive), falling back to the user's `otro` (created if missing). Categories are
never deleted, only deactivated (`active = FALSE`), so old records keep theirs.

## `category_events` (append-only category history)

One row per category change; never updated or deleted. `categories` stays the source of truth;
this table answers "how many times did this user change categories?" and "what was this
category called before?". Seeding a new user writes one `add` event per default category.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` `ON DELETE CASCADE` |
| `category_id` | `INTEGER NOT NULL` | FK → `categories(id)` `ON DELETE CASCADE` |
| `kind` | `VARCHAR(16) NOT NULL` | `expense` or `income` |
| `action` | `VARCHAR(20) NOT NULL` | `add`, `rename`, `merge`, `deactivate`, `reactivate` |
| `old_name` / `new_name` | `VARCHAR(50)` | Before / after the change |
| `records_affected` | `INTEGER` | Records re-pointed by a merge |
| `source` | `VARCHAR(20) NOT NULL` | `seed`, `onboarding`, `users_agent` |
| `created_at` | `TIMESTAMPTZ` | |

## `expenses`

One row per parsed expense.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` PK | |
| `user_id` | `INTEGER NOT NULL` | FK → `users(id)` |
| `message_id` | `INTEGER NOT NULL` | FK → `messages(id)` `ON DELETE CASCADE` |
| `amount` | `NUMERIC(12,2) NOT NULL` | |
| `currency` | `VARCHAR(10) NOT NULL DEFAULT 'COP'` | |
| `category_id` | `INTEGER NOT NULL` | FK → `categories(id)`; the name is read through it |
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
[`core.confidence.estimate_confidence`](../src/finance_tracker/core/confidence.py), **not** a model probability:

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
| `category_id` | `INTEGER NOT NULL` | FK → `categories(id)` |
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
| `idx_user_prefs_latest` | `user_prefs(user_id, created_at DESC, id DESC)` — the latest-row lookup |
| `idx_turns_user_created` | `conversation_turns(user_id, created_at)` |
| `idx_categories_user` | `categories(user_id, kind)` |
| `idx_category_events_user` | `category_events(user_id, created_at)` |
| `idx_expenses_user` | `expenses(user_id)` |
| `idx_expenses_message_id` | `expenses(message_id)` |
| `idx_expenses_date` | `expenses(expense_date)` |
| `idx_incomes_user` | `incomes(user_id)` |
| `idx_incomes_date` | `incomes(income_date)` |
| `idx_expenses_category` / `idx_incomes_category` | `category_id` |
| `idx_budgets_user` | `budgets(user_id)` |

## Reset

Existing data is disposable in this stage. The top of
[`scripts/setup_db.sql`](../scripts/setup_db.sql) has a commented **RESET** block —
uncomment the `DROP TABLE … CASCADE` lines and run once to wipe, then run the `CREATE`
statements. Phase 6 changes the schema without migrations: the database is rebuilt this way
once Phase 6 is complete.

## Common query

Reports and Excel exports read through [`database.get_expenses`](../src/finance_tracker/storage/database.py),
scoped by `user_id`, excluding soft-deleted rows, with the category's current name:

```sql
SELECT e.amount, e.currency, c.name AS category, e.expense_date,
       e.payment_method, e.merchant, e.description, e.source
FROM expenses e
JOIN categories c ON c.id = e.category_id
WHERE e.user_id = :user_id
  AND e.deleted_at IS NULL
  AND e.expense_date BETWEEN :min_date AND :max_date
ORDER BY e.expense_date, c.name;
```
