CREATE TABLE IF NOT EXISTS messages (
    id                      SERIAL PRIMARY KEY,
    whatsapp_message_id     VARCHAR(128) UNIQUE NOT NULL,
    phone_number            VARCHAR(20) NOT NULL,
    raw_text                TEXT NOT NULL,
    created_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS expenses (
    id              SERIAL PRIMARY KEY,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    amount          NUMERIC(12, 2) NOT NULL,
    currency        VARCHAR(10) NOT NULL DEFAULT 'COP',
    category        VARCHAR(50) NOT NULL,
    expense_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    payment_method  VARCHAR(50),
    merchant        VARCHAR(100),
    description     TEXT,
    confidence      NUMERIC(3, 2),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expenses_message_id ON expenses (message_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (expense_date);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses (category);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone_number);

-- Migration for existing deployments: add currency column if missing
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'COP';
