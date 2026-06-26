-- Finance Tracker schema (Phase 0 — multi-agent foundation)
--
-- Existing data is disposable. To rebuild from scratch, uncomment and run the
-- RESET block once, then run the CREATE statements.

-- ── RESET (run once to wipe everything) ─────────────────────────────────────────
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
    prefs       JSONB NOT NULL DEFAULT '{}',
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

-- ── conversation_turns (short-term chatbot memory) ───────────────────────────────
CREATE TABLE IF NOT EXISTS conversation_turns (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL,          -- 'user' | 'assistant'
    content     TEXT NOT NULL,
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

-- ── expenses ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expenses (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category        VARCHAR(50) NOT NULL,
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
    category        VARCHAR(50) NOT NULL,
    income_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    source          VARCHAR(20) NOT NULL DEFAULT 'manual',   -- manual | gmail | bank
    description     TEXT,
    confidence      NUMERIC(3, 2),
    deleted_at      TIMESTAMP WITH TIME ZONE,
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
CREATE INDEX IF NOT EXISTS idx_turns_user_created   ON conversation_turns (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_categories_user      ON categories (user_id, kind);
CREATE INDEX IF NOT EXISTS idx_expenses_user        ON expenses (user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_message_id  ON expenses (message_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date        ON expenses (expense_date);
CREATE INDEX IF NOT EXISTS idx_incomes_user         ON incomes (user_id);
CREATE INDEX IF NOT EXISTS idx_incomes_date         ON incomes (income_date);
CREATE INDEX IF NOT EXISTS idx_budgets_user         ON budgets (user_id);
