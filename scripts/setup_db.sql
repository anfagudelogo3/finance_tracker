-- Finance Tracker schema (Phase 0 — multi-agent foundation)
--
-- Existing data is disposable. To rebuild from scratch, uncomment and run the
-- RESET block once, then run the CREATE statements.

-- ── RESET (run once to wipe everything) ─────────────────────────────────────────
-- DROP TABLE IF EXISTS user_prefs CASCADE;
-- DROP TABLE IF EXISTS record_events CASCADE;
-- DROP TABLE IF EXISTS category_events CASCADE;
-- DROP TABLE IF EXISTS budgets CASCADE;
-- DROP TABLE IF EXISTS incomes CASCADE;
-- DROP TABLE IF EXISTS expenses CASCADE;
-- DROP TABLE IF EXISTS categories CASCADE;
-- DROP TABLE IF EXISTS conversation_turns CASCADE;
-- DROP TABLE IF EXISTS messages CASCADE;
-- DROP TABLE IF EXISTS users CASCADE;

-- ── users ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    phone       VARCHAR(20) UNIQUE NOT NULL,
    name        VARCHAR(100),
    prefs       JSONB NOT NULL DEFAULT '{}',              -- DEPRECATED: superseded by user_prefs
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── user_prefs (append-only preference history) ─────────────────────────────────
-- Every preference change INSERTs a new row with the FULL preferences snapshot; rows are
-- never updated or deleted. The current preferences of a user are their latest row
-- (ORDER BY created_at DESC, id DESC LIMIT 1). `changes` holds only the keys that changed.
CREATE TABLE IF NOT EXISTS user_prefs (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prefs       JSONB NOT NULL,                           -- full snapshot after the change
    changes     JSONB,                                    -- the changed keys only
    source      VARCHAR(20) NOT NULL DEFAULT 'manual',    -- onboarding | onboarding_restart | users_agent | manual
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── messages (raw inbound audit log) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id                      SERIAL PRIMARY KEY,
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    whatsapp_message_id     VARCHAR(128) UNIQUE NOT NULL,
    phone_number            VARCHAR(20) NOT NULL,
    raw_text                TEXT NOT NULL,
    transcript              TEXT,
    created_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── conversation_turns (short-term chatbot memory + reply context) ───────────────
-- external_id: the Twilio SID of an outbound (assistant) message, so a WhatsApp swipe-reply
-- to it can be resolved. message_id: the inbound message this turn is (user) or answers
-- (assistant) — records carry the same message_id, which links a confirmation to its records.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL,          -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    external_id VARCHAR(64) UNIQUE,            -- Twilio SID of an outbound message
    message_id  INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── categories (custom per user) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(50) NOT NULL,
    kind        VARCHAR(16) NOT NULL DEFAULT 'expense',   -- 'expense' | 'income'
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (user_id, name, kind)
);

-- ── category_events (append-only category history) ──────────────────────────────
-- One row per category change (add, rename, merge, deactivate, reactivate); never updated.
-- `categories` stays the source of truth the agents read; this is the audit trail.
CREATE TABLE IF NOT EXISTS category_events (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id         INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    kind                VARCHAR(16) NOT NULL,                 -- 'expense' | 'income'
    action              VARCHAR(20) NOT NULL,                 -- add | rename | merge | deactivate | reactivate
    old_name            VARCHAR(50),
    new_name            VARCHAR(50),
    records_affected    INTEGER,                              -- rows re-pointed by a merge
    source              VARCHAR(20) NOT NULL,                 -- seed | onboarding | users_agent
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── expenses ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expenses (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category_id     INTEGER NOT NULL REFERENCES categories(id),
    expense_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    payment_method  VARCHAR(50),
    merchant        VARCHAR(100),
    description     TEXT,
    confidence      NUMERIC(3, 2),
    source          VARCHAR(10) NOT NULL DEFAULT 'text',
    deleted_at      TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── incomes (table created now; agent added in Phase 2) ──────────────────────────
CREATE TABLE IF NOT EXISTS incomes (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category_id     INTEGER NOT NULL REFERENCES categories(id),
    income_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    source          VARCHAR(20) NOT NULL DEFAULT 'manual',   -- manual | gmail | bank
    description     TEXT,
    confidence      NUMERIC(3, 2),
    deleted_at      TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── record_events (append-only history of every expense/income change) ──────────
-- One row per create, update, delete, undo or move (expense ↔ income); never updated or
-- deleted. `before` / `after` hold the record's field values, so it's both the audit
-- trail and what undo reverts to. created_at on expenses/incomes is never edited.
-- Design: docs/design/phase-8-corrections.md.
CREATE TABLE IF NOT EXISTS record_events (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    record_kind     VARCHAR(10) NOT NULL,                 -- 'expense' | 'income'
    record_id       INTEGER NOT NULL,
    action          VARCHAR(20) NOT NULL,                 -- create | update | delete | undo | move
    before          JSONB,                                -- NULL for create
    after           JSONB,                                -- NULL for delete
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,  -- what caused it
    related_event   INTEGER REFERENCES record_events(id), -- other half of a move / event an undo reverts
    source          VARCHAR(20) NOT NULL,                 -- expense_agent | income_agent | image_parser | undo
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── budgets (per-currency; table created now, agent added in Phase 3) ─────────────
CREATE TABLE IF NOT EXISTS budgets (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope       VARCHAR(20) NOT NULL DEFAULT 'global',    -- 'global' | 'category'
    category    VARCHAR(50),                              -- NULL when scope = 'global'
    currency    VARCHAR(10) NOT NULL DEFAULT 'COP',
    period      VARCHAR(16) NOT NULL DEFAULT 'monthly',   -- 'monthly' | 'weekly'
    amount      NUMERIC(12, 2) NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ── indexes ───────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_messages_user        ON messages (user_id);
CREATE INDEX IF NOT EXISTS idx_user_prefs_latest    ON user_prefs (user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_turns_user_created   ON conversation_turns (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_categories_user      ON categories (user_id, kind);
CREATE INDEX IF NOT EXISTS idx_category_events_user ON category_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_record_events_record ON record_events (record_kind, record_id);
CREATE INDEX IF NOT EXISTS idx_record_events_user   ON record_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_expenses_user        ON expenses (user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_message_id  ON expenses (message_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date        ON expenses (expense_date);
CREATE INDEX IF NOT EXISTS idx_incomes_user         ON incomes (user_id);
CREATE INDEX IF NOT EXISTS idx_incomes_date         ON incomes (income_date);
CREATE INDEX IF NOT EXISTS idx_expenses_category    ON expenses (category_id);
CREATE INDEX IF NOT EXISTS idx_incomes_category     ON incomes (category_id);
CREATE INDEX IF NOT EXISTS idx_budgets_user         ON budgets (user_id);

