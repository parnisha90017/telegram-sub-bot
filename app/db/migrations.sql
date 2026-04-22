CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    plan        TEXT,
    paid_until  TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'new',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    plan        TEXT NOT NULL,
    amount      NUMERIC(12, 2) NOT NULL,
    payment_id  TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_paid_until_active
    ON users(paid_until) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_payments_telegram_id
    ON payments(telegram_id);
