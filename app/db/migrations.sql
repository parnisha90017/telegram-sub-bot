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

-- Heleket integration: provider column + composite uniqueness.
-- Idempotent: ADD COLUMN IF NOT EXISTS, DROP CONSTRAINT IF EXISTS for both
-- the legacy auto-name and the new composite name.

ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'cryptobot';

ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_payment_id_key;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_provider_payment_id_key;
ALTER TABLE payments ADD CONSTRAINT payments_provider_payment_id_key UNIQUE (provider, payment_id);

CREATE INDEX IF NOT EXISTS idx_payments_provider_status ON payments(provider, status);

-- Reuse pending invoice on repeated tariff click (Heleket возвращает тот же
-- payment_id для того же order_id → ловили UniqueViolationError на INSERT).
-- Храним pay_url у каждого pending, чтобы переиспользовать без второго
-- похода в провайдер. Legacy-записи до миграции — pay_url IS NULL,
-- find_active_pending_payment их игнорирует, со 2-го клика юзера
-- ON CONFLICT DO UPDATE их «починит».
ALTER TABLE payments ADD COLUMN IF NOT EXISTS pay_url TEXT;

CREATE INDEX IF NOT EXISTS idx_payments_pending_lookup
    ON payments(telegram_id, plan, provider, status);

-- granted_access: связывает invite-ссылку (creates_join_request=True) с покупателем.
-- Используется для approve/decline в ChatJoinRequest и для kick по реальным
-- участникам (не по users.telegram_id).
CREATE TABLE IF NOT EXISTS granted_access (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,           -- покупатель (кому выдана ссылка)
    chat_id     BIGINT NOT NULL,           -- канал
    invite_link TEXT NOT NULL UNIQUE,      -- сама ссылка (match при join_request)
    paid_until  TIMESTAMPTZ NOT NULL,      -- срок доступа
    joined_at   TIMESTAMPTZ,               -- когда юзер реально вошёл (NULL до)
    revoked_at  TIMESTAMPTZ,               -- когда кикнут (NULL пока активен)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_granted_access_invite_link ON granted_access(invite_link);
CREATE INDEX IF NOT EXISTS idx_granted_access_telegram ON granted_access(telegram_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_granted_access_paid_until
    ON granted_access(paid_until) WHERE revoked_at IS NULL;

-- Legacy import: invite_link становится NULL-able (записи с
-- source='legacy_import' создаются без реальной Telegram-ссылки),
-- + колонка source отделяет «реальный invite-flow» от ручного импорта.
-- UNIQUE(invite_link) остаётся: в Postgres NULL не конфликтует с NULL.
ALTER TABLE granted_access ALTER COLUMN invite_link DROP NOT NULL;
ALTER TABLE granted_access
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'invite_link';

-- User-initiated reissue: rate-limit (1 раз / час) хранится здесь,
-- чтобы юзер не мог нажать кнопку «Получить ссылки заново» подряд.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_reissue_at TIMESTAMPTZ;
