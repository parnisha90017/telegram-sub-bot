# Архитектура: telegram-sub-bot

## Context

Проект — Telegram-бот подписочной системы с крипто-оплатой через **CryptoPay (@CryptoBot)**, только USDT. Пользователь платит — получает 4 одноразовые invite-ссылки в приватные чаты. По истечении срока его киксают из всех 4 чатов, за 24 часа до конца — напоминание.

Состояние репозитория на момент написания документа: чистый лист. Есть только настроенные скиллы в `.claude/skills/` и `skills-lock.json`.

### Источники контрактов

- **aiogram 3.x** (`/websites/aiogram_dev_en_v3_27_0`): `SimpleRequestHandler(dispatcher, bot, secret_token)` + `setup_application(app, dp, bot=bot)`; `create_chat_invite_link(chat_id, expire_date, member_limit=1..99999)`, бот должен быть админом.
- **CryptoPay API** (`https://pay.crypt.bot/api/`):
  - Авторизация исходящих запросов: заголовок `Crypto-Pay-API-Token: <app_token>`.
  - Верификация входящего webhook: заголовок `crypto-pay-api-signature` = `HMAC-SHA256(key=SHA256(app_token), body_raw)`.
  - `createInvoice`: `amount`, `asset="USDT"`, `description`, `payload` (≤ 4 КБ), `paid_btn_name="callback"`, `paid_btn_url`, `expires_in` (секунды).
  - Ответ инвойса: `invoice_id`, `bot_invoice_url`, `status ∈ {active, paid, expired}`.
  - Webhook: JSON с `update_type="invoice_paid"`, `payload=Invoice`.
  - Поля `Invoice`: `invoice_id`, `amount`, `status`, `payload`, `paid_at`, `fee_amount`.
- **aiocryptopay** (`/layerqa/aiocryptopay`) — Python-обёртка, скрывает оба заголовка. `AioCryptoPay(token, network)`, `create_invoice(...)`, `@crypto.pay_handler()`, `web.post('/...', crypto.get_updates)`, `crypto.check_signature(body_text, crypto_pay_signature)` — встроенная верификация.

---

## Принятые решения и значения по умолчанию

- **Крипто-актив:** `asset="USDT"` (в @CryptoBot это USDT на TON или TRC20 — выбирает плательщик).
- **Сеть:** переменная `CRYPTO_PAY_NETWORK` (`main` → `Networks.MAIN_NET` → `https://pay.crypt.bot`; `test` → `Networks.TEST_NET` → `https://testnet-pay.crypt.bot`). Для разработки — `test`.
- **Кнопка после оплаты:** `paid_btn_name="callback"`, `paid_btn_url="https://t.me/<BOT_USERNAME>"` — возврат в наш бот.
- **Webhook-handler:** встроенный `@crypto.pay_handler()` + `web.post('/cryptopay/webhook', crypto.get_updates)`. Идемпотентность и транзакция БД — внутри тела handler'а.
- **`/start` для активного подписчика:** показываем `подписка активна до YYYY-MM-DD HH:MM UTC` и всё равно даём 3 кнопки (продление суммируется).
- **Одиночный инстанс приложения**, APScheduler в том же процессе, без jobstore.
- **Таймзона БД и планировщика — UTC**.
- **Админские команды** — не делаем в MVP.
- **Авто-отмена неоплаченных инвойсов** — не чистим, CryptoPay сам переведёт в `expired` через `expires_in=3600`.

---

## 1. Архитектура системы

### Компоненты

```
┌──────────────────────────────────────────────────────────────────┐
│                    Один процесс Python (app/main.py)             │
│                                                                  │
│  ┌─────────────────┐   ┌──────────────────────┐  ┌────────────┐  │
│  │ aiogram         │   │ aiohttp web.App      │  │ APScheduler│  │
│  │ Dispatcher +    │◄──┤ /tg/webhook          │  │ AsyncIO    │  │
│  │ Routers         │   │ /cryptopay/webhook   │  │ remind_24h │  │
│  └────────┬────────┘   └──────────┬───────────┘  │ kick_expir.│  │
│           │                       │              └──────┬─────┘  │
│           ▼                       ▼                     ▼        │
│     Bot handlers        aiocryptopay.get_updates   Scheduled    │
│     (start, buy)        → @crypto.pay_handler()    jobs         │
│           │                       │                     │        │
│           └──────────┬────────────┴─────────┬───────────┘        │
│                     ▼                      ▼                     │
│                 ┌──────────┐          ┌──────────┐               │
│                 │ services │          │ asyncpg  │               │
│                 │ chats/   │          │ Pool     │               │
│                 │ payments │          │ users    │               │
│                 └─────┬────┘          │ payments │               │
│                       │               └────┬─────┘               │
└───────────────────────┼────────────────────┼────────────────────┘
                        │                    │
                        ▼                    ▼
            ┌────────────────────┐   ┌──────────────────┐
            │ Telegram Bot API   │   │ PostgreSQL 15+   │
            │ (send,invite,ban)  │   │                  │
            └─────────┬──────────┘   └──────────────────┘
                      │
                      ▼
              ┌─────────────┐     ┌─────────────────────────────┐
              │ 4 чата      │     │ CryptoPay API               │
              │ (приватные) │     │ pay.crypt.bot/api/          │
              └─────────────┘     │ header: Crypto-Pay-API-Token │
                                  └─────────────────────────────┘
```

### Потоки данных

1. **Исходящий к CryptoPay:** пользователь жмёт кнопку → `payment.py` зовёт `payments/cryptopay.create_invoice_for` → `AioCryptoPay.create_invoice()` (под капотом — `POST https://pay.crypt.bot/api/createInvoice` с `Crypto-Pay-API-Token`) → ответ с `invoice_id` и `bot_invoice_url`.
2. **Входящий от CryptoPay:** CryptoPay POST'ит на `/cryptopay/webhook` → `crypto.get_updates` верифицирует подпись (HMAC-SHA256) → вызывает наш `@crypto.pay_handler()` → idempotency-check по `invoice_id` → транзакция в БД → `issue_invite_links_and_send` → 4 ссылки в ЛС.
3. **Внутренний крон:** `remind_24h` ежечасно, `kick_expired` каждые 10 минут.
4. **Telegram → бот:** POST на `/tg/webhook` c `X-Telegram-Bot-Api-Secret-Token` → Dispatcher → router.

---

## 2. User flow

```
[Пользователь]               [Бот]                      [CryptoPay]       [БД]
     │                         │                            │               │
     │  /start                 │                            │               │
     ├────────────────────────►│                            │               │
     │                         │  UPSERT users                              │
     │                         ├───────────────────────────────────────────►│
     │  приветствие + 3 кнопки │                            │               │
     │◄────────────────────────┤                            │               │
     │                         │                            │               │
     │  callback buy:tariff_7d │                            │               │
     ├────────────────────────►│                            │               │
     │                         │  createInvoice(            │               │
     │                         │    asset="USDT", amount=21,│               │
     │                         │    description="7 дней",   │               │
     │                         │    payload="<tg>:<plan>",  │               │
     │                         │    expires_in=3600,        │               │
     │                         │    paid_btn_name="callback",│              │
     │                         │    paid_btn_url="https://t.me/<bot>")      │
     │                         ├───────────────────────────►│               │
     │                         │  Invoice(invoice_id,bot_invoice_url,active)│
     │                         │◄───────────────────────────┤               │
     │                         │  INSERT payment (pending, invoice_id)      │
     │                         ├───────────────────────────────────────────►│
     │  «Оплатите по ссылке»   │                            │               │
     │  (ссылка на @CryptoBot) │                            │               │
     │◄────────────────────────┤                            │               │
     │                         │                            │               │
     │  оплата через @CryptoBot → CryptoPay фиксирует tx    │               │
     │                         │  POST /cryptopay/webhook   │               │
     │                         │  Header: crypto-pay-api-signature          │
     │                         │  Body: {update_type:"invoice_paid",        │
     │                         │         payload:{invoice_id,status:"paid", │
     │                         │                  amount,paid_at,...}}      │
     │                         │◄───────────────────────────┤               │
     │                         │  get_updates:                              │
     │                         │    secret = SHA256(CRYPTO_PAY_TOKEN)       │
     │                         │    HMAC-SHA256(secret, body) == header ?   │
     │                         │      нет → 401                             │
     │                         │      да  → @pay_handler(update)            │
     │                         │  SELECT payment WHERE payment_id           │
     │                         ├───────────────────────────────────────────►│
     │                         │  уже paid → return (idempotent)            │
     │                         │  UPDATE payment → paid                     │
     │                         │  UPDATE users.paid_until += N дней         │
     │                         ├───────────────────────────────────────────►│
     │                         │  create_chat_invite_link × 4               │
     │                         │  (member_limit=1, expire=+1ч)              │
     │  4 invite-ссылки в ЛС   │                            │               │
     │◄────────────────────────┤                            │               │
     │  переходит и вступает в 4 чата                       │               │
     │                         │                            │               │
     │  ... за 24ч до конца    │   (remind_24h)             │               │
     │◄────────────────────────┤                            │               │
     │                         │                            │               │
     │  ... через срок         │   (kick_expired)           │               │
     │                         │  UPDATE users.status=expired                │
     │                         │  ban_chat_member × 4 + unban                │
     │  исключён из 4 чатов    │                            │               │
```

---

## 3. Структура файлов проекта

```
telegram-sub-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                 # точка входа; собирает aiohttp app, Dispatcher, Scheduler, AioCryptoPay
│   ├── config.py               # pydantic-settings: BOT_TOKEN, BOT_USERNAME, DATABASE_URL,
│   │                           # CRYPTO_PAY_TOKEN, CRYPTO_PAY_NETWORK, CHAT_IDS,
│   │                           # TELEGRAM_WEBHOOK_URL, TELEGRAM_WEBHOOK_SECRET, CRYPTO_PAY_WEBHOOK_URL
│   │
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── handlers/
│   │   │   ├── __init__.py
│   │   │   ├── start.py        # /start: приветствие + статус подписки + inline-меню
│   │   │   └── payment.py      # callback "buy:<plan_key>": создание invoice, запись pending
│   │   ├── keyboards.py        # plans_kb() + словарь PLANS: {plan_key: (title, amount, days)}
│   │   └── texts.py            # пользовательские строки (ru)
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── pool.py             # init_pool/close_pool, asyncpg.Pool
│   │   ├── migrations.sql      # CREATE TABLE IF NOT EXISTS users, payments + индексы
│   │   └── queries.py          # upsert_user, insert_pending_payment, get_payment_by_invoice_id,
│   │                           # mark_payment_paid, extend_subscription, list_expiring_between,
│   │                           # expire_and_return_ids
│   │
│   ├── payments/
│   │   ├── __init__.py
│   │   ├── cryptopay.py        # build_client(settings) -> AioCryptoPay
│   │   │                       # create_invoice_for(client, tg_id, plan_key) -> (invoice_id, url)
│   │   │                       # encode_payload/decode_payload
│   │   └── webhook.py          # register_cryptopay_handlers(crypto, bot):
│   │                           #   @crypto.pay_handler() on_invoice_paid(update, app)
│   │
│   ├── chats/
│   │   ├── __init__.py
│   │   └── manager.py          # issue_invite_links_and_send(bot, tg_id),
│   │                           # kick_from_all_chats(bot, tg_id)
│   │
│   └── scheduler/
│       ├── __init__.py
│       └── jobs.py             # setup_scheduler(bot), remind_24h, kick_expired
│
├── tests/                      # pytest-asyncio
│   ├── test_extend_subscription.py
│   ├── test_webhook_idempotency.py
│   ├── test_issue_invite_links.py
│   └── test_payload_encoding.py
│
├── docs/
│   └── plans/
│       └── architecture.md     # этот документ
│
├── .env.example
├── requirements.txt            # aiogram>=3.4, aiocryptopay, asyncpg, aiohttp, APScheduler, pydantic-settings
├── Dockerfile                  # python:3.11-slim
├── docker-compose.yml          # app + postgres
├── README.md
└── .gitignore
```

**Ответственности:**
- `main.py` — проводка: один общий `AioCryptoPay`-клиент создаётся здесь и пробрасывается в `webhook.py`. Без бизнес-логики.
- `bot/handlers/*` — только Telegram-реакции; SQL не инлайнится.
- `db/queries.py` — единственное место с SQL.
- `payments/cryptopay.py` — всё про CryptoPay на уровне API (создание клиента, создание инвойса, сериализация `payload`).
- `payments/webhook.py` — регистрирует `@crypto.pay_handler()`, оркестрирует post-payment flow.
- `chats/manager.py` — всё, что трогает чаты через bot API.
- `scheduler/jobs.py` — периодические задачи.

---

## 4. Схема БД

```sql
-- app/db/migrations.sql

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    plan        TEXT,                              -- 'tariff_3d' | 'tariff_7d' | 'tariff_30d' | NULL
    paid_until  TIMESTAMPTZ,                       -- NULL если ни разу не платил
    status      TEXT NOT NULL DEFAULT 'new',       -- 'new' | 'active' | 'expired'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    plan        TEXT NOT NULL,                     -- 'tariff_3d' | 'tariff_7d' | 'tariff_30d'
    amount      NUMERIC(12, 2) NOT NULL,           -- USDT
    payment_id  TEXT NOT NULL UNIQUE,              -- CryptoPay invoice_id (строкой) — ключ идемпотентности
    status      TEXT NOT NULL,                     -- 'pending' | 'paid' | 'failed' | 'expired'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_paid_until_active
    ON users(paid_until) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_payments_telegram_id
    ON payments(telegram_id);
```

**Почему так:**
- Колонка `payment_id` хранит `str(invoice_id)` (CryptoPay возвращает `int`, но TEXT проще расширять).
- `UNIQUE(payment_id)` — защита от повторной обработки при retry webhook.
- Частичный индекс покрывает единственный сценарий поиска по `paid_until` (крон).
- `FOREIGN KEY` — каждый платёж привязан к юзеру (`upsert_user` вызывается до `insert_pending_payment`).
- `TIMESTAMPTZ` везде; приложение работает в UTC.
- `NUMERIC(12,2)` — без плавающей запятой.

**Миграционная стратегия:** один файл `migrations.sql`, применяется один раз при деплое (`psql -f`). Alembic — не сейчас (YAGNI).

---

## 5. API контракты

### 5.1. Создание инвойса

**Наш код (через aiocryptopay):**
```python
invoice = await crypto.create_invoice(
    asset="USDT",                          # Assets.USDT
    amount=21,
    description="Подписка 7 дней",
    payload=encode_payload(tg_id, plan_key),   # "<tg>:<plan>" (≤ 4 КБ)
    expires_in=3600,
    paid_btn_name="callback",              # PaidButtons.CALLBACK
    paid_btn_url=f"https://t.me/{BOT_USERNAME}",
    allow_comments=False,
    allow_anonymous=False,
)
```

**Низкоуровневый HTTP (что делает библиотека, для прозрачности):**
```
POST https://pay.crypt.bot/api/createInvoice
Headers:
  Content-Type:           application/json
  Crypto-Pay-API-Token:   <CRYPTO_PAY_TOKEN>
Body:
  {
    "asset":"USDT","amount":"21","description":"Подписка 7 дней",
    "payload":"123:tariff_7d","expires_in":3600,
    "paid_btn_name":"callback","paid_btn_url":"https://t.me/mybot",
    "allow_comments":false,"allow_anonymous":false
  }
```

**Поля `Invoice`, которыми пользуемся:**
| Поле | Тип | Использование |
|------|-----|---------------|
| `invoice_id` | int | → `payments.payment_id = str(invoice_id)` |
| `bot_invoice_url` | str | отправляем пользователю |
| `status` | str | при создании — `active` |
| `payload` | str | наша метка `"<tg>:<plan>"`; вернётся в webhook |
| `amount` | str | сверяем с `PLANS[plan_key].amount` |
| `paid_at` | datetime? | в webhook — время оплаты |
| `fee_amount` | str? | для логов/аналитики, не бизнес-логика |

### 5.2. Webhook Update

**HTTP-уровень (что CryptoPay шлёт нам):**
```
POST https://<host>/cryptopay/webhook
Headers:
  Content-Type:               application/json
  crypto-pay-api-signature:   <hex HMAC-SHA256>
Body:
  {
    "update_id":     123,
    "update_type":   "invoice_paid",
    "request_date":  "2026-04-22T12:00:00Z",
    "payload": {
      "invoice_id":  123456,
      "status":      "paid",
      "amount":      "21",
      "payload":     "123:tariff_7d",
      "paid_at":     "2026-04-22T12:00:00Z",
      "fee_amount":  "0.042",
      ...
    }
  }
```

**Ответ нашего сервера:**
- `200 OK` — всегда при валидной подписи (включая «уже обработано» и unknown invoice_id). Сигнал CryptoPay прекратить retry.
- `401` — отдаёт библиотека при невалидной подписи.
- `500` — необработанная ошибка → CryptoPay будет ретраить.

**`update_type`:** обрабатываем только `invoice_paid`. Библиотека роутит по типу — прочие типы просто не дойдут до нашего handler'а, ответ 200.

### 5.3. Подпись webhook (HMAC-SHA256)

Алгоритм (встроен в `aiocryptopay.check_signature`):
```
secret    = sha256(CRYPTO_PAY_TOKEN_utf8).digest()        # 32 байта
expected  = hmac_sha256(secret, body_raw_bytes).hexdigest()  # hex-string
match     = hmac.compare_digest(expected, request.headers['crypto-pay-api-signature'])
```

**Важно:**
- `body_raw_bytes` — **исходные байты тела запроса** (не перекодированный JSON). Библиотека делает `await request.read()` и сравнивает с заголовком. Любая пере-сериализация сломает подпись → middleware, которое парсит body, прикручивать нельзя.
- Сравнение через `hmac.compare_digest` — защита от timing-атак.
- Мы **не пишем** HMAC сами. Для unit-тестов можем вызвать `crypto.check_signature(body_text, sig)` явно.

### 5.4. Авторизация исходящих запросов

- Заголовок: `Crypto-Pay-API-Token: <CRYPTO_PAY_TOKEN>`.
- Токены у `main` и `test` — **разные**, выдают соответственно `@CryptoBot` и `@CryptoTestnetBot`. Переменная `CRYPTO_PAY_NETWORK` определяет, к какому серверу обращаемся.

---

## 6. Webhook flow (/cryptopay/webhook)

```
┌─────────────────────────────────────────────────────────────────┐
│  POST /cryptopay/webhook                                         │
└──────────┬──────────────────────────────────────────────────────┘
           │
           ▼
   crypto.get_updates(request):           # aiocryptopay
       │
       ├─ read body_raw
       │
       ├─ verify: hmac_sha256(sha256(token), body) == header ?
       │       └─ False → return 401
       │
       ├─ parse Update (pydantic)
       │
       └─ dispatch по update_type → наш @pay_handler
                 │
                 ▼
   @crypto.pay_handler()
   async def on_invoice_paid(update, app):
      invoice = update.payload                        # Invoice object
      invoice_id_str = str(invoice.invoice_id)

      if invoice.status != "paid":
          return   # defensive

      async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT telegram_id, plan, amount, status FROM payments "
                "WHERE payment_id = $1 FOR UPDATE",
                invoice_id_str)

            if row is None:
                log.warning("unknown invoice %s", invoice_id_str)
                return                               # 200 от библиотеки

            if row["status"] == "paid":
                return                               # идемпотентность

            if Decimal(str(invoice.amount)) != row["amount"]:
                log.error("amount mismatch on %s", invoice_id_str)
                return

            await conn.execute(
                "UPDATE payments SET status='paid' WHERE payment_id=$1",
                invoice_id_str)

            await conn.execute("""
                UPDATE users
                   SET plan = $2,
                       paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                                  + ($3 || ' days')::interval,
                       status = 'active'
                 WHERE telegram_id = $1
            """, row["telegram_id"], row["plan"], PLAN_DAYS[row["plan"]])

        tg_id = row["telegram_id"]

      # Вне транзакции — Telegram I/O:
      await issue_invite_links_and_send(bot, tg_id)
```

**Инварианты:**
- Подпись проверяется библиотекой до входа в handler.
- `FOR UPDATE` на строке `payments` — атомарная защита от одновременной обработки двух webhook'ов с одним `invoice_id`.
- Транзакция БД — внутри handler'а; `issue_invite_links_and_send` — **вне** транзакции.
- Источник истины для `telegram_id` и `plan` — **наша запись в `payments`**, не `invoice.payload`. Payload — вторичная метка для логов.

---

## 7. Крон-задачи

### 7.1. `remind_24h` — напоминание за 24 часа

**Расписание:** каждый час (`interval, hours=1`).

**Алгоритм:**
```
now = UTC.now
SELECT telegram_id FROM users
  WHERE status = 'active'
    AND paid_until BETWEEN now + 23h30m AND now + 24h30m

for each:
    try bot.send_message(tg_id, "подписка истекает через ~24 часа")
    except TelegramForbiddenError: pass
    except TelegramRetryAfter:   sleep(retry_after) & retry one
```

Окно ±30 мин вокруг 24h полностью покрывает часовой слот и не даёт двойных напоминаний.

### 7.2. `kick_expired` — удаление просроченных

**Расписание:** каждые 10 минут.

**Алгоритм:**
```
UPDATE users
   SET status = 'expired'
 WHERE status = 'active' AND paid_until < NOW()
RETURNING telegram_id;

for each tg_id:
    for chat_id in CHAT_IDS:
        try:
            await bot.ban_chat_member(chat_id, tg_id)
            await bot.unban_chat_member(chat_id, tg_id)
        except TelegramBadRequest: pass
        except TelegramRetryAfter: sleep(retry_after) & retry
        await asyncio.sleep(0.1)
```

`ban`+`unban` — чистое удаление без постоянного чёрного списка. `RETURNING` гарантирует, что параллельные итерации не возьмут строку дважды.

---

## 8. Порядок реализации

### Фаза 1. Каркас
1. `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`.
2. `app/config.py` — все переменные окружения, валидация `CHAT_IDS` (ровно 4), `CRYPTO_PAY_NETWORK ∈ {main, test}`.
3. `app/db/migrations.sql` + `app/db/pool.py`.
4. `app/main.py` — скелет: aiohttp + `/health` + init_pool в on_startup.
5. Контроль: `docker compose up` → `/health` = 200, таблицы созданы.

### Фаза 2. Бот-скелет на webhook
6. `app/bot/keyboards.py` + `PLANS`.
7. `app/bot/texts.py`.
8. `app/bot/handlers/start.py` — `/start` с upsert, статусом и меню.
9. `app/db/queries.py` — `upsert_user`, `get_user_by_tg_id`.
10. `app/main.py` — `SimpleRequestHandler` на `/tg/webhook` с `secret_token`, `set_webhook` в on_startup.
11. Контроль: `/start` через ngrok отвечает меню.

### Фаза 3. CryptoPay: создание инвойса
12. `app/payments/cryptopay.py` — `build_client(settings)`, `encode_payload / decode_payload`, `create_invoice_for(client, tg_id, plan_key) -> (invoice_id, url)`.
13. `app/db/queries.py` — `insert_pending_payment`, `get_payment_by_invoice_id`.
14. `app/bot/handlers/payment.py` — callback `buy:<plan>` → create_invoice → ответ с URL.
15. `app/main.py` — создаём `AioCryptoPay` в on_startup, `await crypto.close()` в on_cleanup.
16. Контроль: кнопка даёт ссылку `https://t.me/CryptoBot?...`, в `payments` строка `pending`.

### Фаза 4. Webhook CryptoPay и продление
17. `app/db/queries.py` — `mark_payment_paid`, `extend_subscription` + unit-тесты (новый / истёк / активен).
18. `app/chats/manager.py` — `issue_invite_links_and_send` (unit-тест с mock-bot).
19. `app/payments/webhook.py` — `register_cryptopay_handlers(crypto, bot)` регистрирует `@crypto.pay_handler()`.
20. `app/main.py` — `web.post('/cryptopay/webhook', crypto.get_updates)`.
21. Контроль: тест-оплата в `Networks.TEST_NET` — webhook отработал, 4 ссылки пришли.

### Фаза 5. Крон
22. `queries`: `list_expiring_between`, `expire_and_return_ids`.
23. `chats/manager.kick_from_all_chats` + тесты.
24. `scheduler/jobs.py` — `remind_24h`, `kick_expired`, `setup_scheduler`.
25. `app/main.py` — старт/стоп scheduler в on_startup/on_cleanup.
26. Контроль: `paid_until = now + 5 минут` + сокращённый интервал `kick_expired` → kick; `paid_until = now + 24ч + 5мин` → напоминание.

### Фаза 6. Полировка
27. README (локальный запуск, деплой, env).
28. Логи (stdlib + JSON-форматтер); маскируем токены.
29. На старте — `get_chat_member(chat_id, bot.id)` для каждого из 4 чатов → warn при отсутствии прав.

**Приоритет для MVP:** 1 → 2 → 3 → 4. Фаза 5 критична, но первые 4 уже закрывают основной цикл продажи.

---

## 9. Деплой

### Минимальные требования
- Ubuntu 22.04+, 1 vCPU, 1 GB RAM.
- Публичный домен + HTTPS.
- Docker + compose (рекомендуется), либо Python 3.11+ + systemd.
- PostgreSQL 15+.
- Reverse proxy для HTTPS (nginx / Caddy / Traefik).

### Переменные окружения (.env)
```
BOT_TOKEN=123:ABC
BOT_USERNAME=my_sub_bot                          # без @
DATABASE_URL=postgresql://user:pass@host:5432/subbot

CRYPTO_PAY_TOKEN=<token от @CryptoBot → Crypto Pay → My Apps>
CRYPTO_PAY_NETWORK=main                          # или test
CRYPTO_PAY_WEBHOOK_URL=https://bot.example.com/cryptopay/webhook

TELEGRAM_WEBHOOK_URL=https://bot.example.com/tg/webhook
TELEGRAM_WEBHOOK_SECRET=<random-32-bytes>

CHAT_IDS=-1001111111111,-1002222222222,-1003333333333,-1004444444444
PORT=8080
LOG_LEVEL=INFO
```

### Развёртывание (Docker)
```
1. git clone ...
2. cp .env.example .env   # заполнить
3. docker compose up -d --build
4. docker compose exec app psql $DATABASE_URL -f app/db/migrations.sql
5. curl https://bot.example.com/health       # 200
```

### Настройка во внешних системах
- **Telegram:** `setWebhook` — автоматически в `on_startup`.
- **CryptoPay (@CryptoBot → Crypto Pay → My Apps → Webhooks):** включить webhook, указать `https://bot.example.com/cryptopay/webhook`. Токен скопировать в `CRYPTO_PAY_TOKEN`.
- **4 чата:** бот — админ с правами `can_invite_users` и `can_restrict_members` в каждом.

### Правила эксплуатации
- **Один инстанс** (иначе крон задвоится).
- **Рестарт безопасен** — state в БД.
- **Логи** → stdout контейнера.

---

## 10. Риски и edge cases

| # | Сценарий | Как закрыто |
|---|----------|-------------|
| 1 | **Двойная оплата / повторный webhook** (CryptoPay ретраит) | `UNIQUE(payment_id)` + `SELECT ... FOR UPDATE` + early-return при `status='paid'` |
| 2 | **Webhook пришёл до записи `pending`** | `INSERT pending` до отправки URL юзеру. При гонке — handler возвращает `200` (unknown invoice), CryptoPay ретраит, ко второй попытке строка уже есть |
| 3 | **Поддельный webhook** | HMAC-SHA256 по `crypto-pay-api-signature`, встроено в `crypto.get_updates`, использует сырое тело |
| 4 | **Бот не админ в одном из 4 чатов** | `create_chat_invite_link` → ошибка → ловим per-chat, логируем, **продолжаем**; юзеру уходят ссылки из оставшихся чатов. В фазе 6 — предстарт-check прав |
| 5 | **Юзер уже в чате при повторной оплате** | Новая invite-ссылка генерится; при переходе Telegram ничего не делает. Не проблема |
| 6 | **Юзер заблокировал бота** | `bot.send_message` → `TelegramForbiddenError`; подписка зачислена, ссылок не получит. Принимаем как известное ограничение; `/links` для повторной выдачи — в беклог |
| 7 | **Rate limit при массовом kick** | `asyncio.sleep(0.1)` между чатами; `TelegramRetryAfter` — уважаем `retry_after` |
| 8 | **Продление при истёкшей подписке** | `GREATEST(COALESCE(paid_until, NOW()), NOW()) + days`: если истекла — стартуем от NOW |
| 9 | **Два инстанса** | Крон задвоится. В README — «только один инстанс». В будущем — `FOR UPDATE SKIP LOCKED` в `expire_and_return_ids` и SQLAlchemyJobStore |
| 10 | **Webhook URL недоступен** | CryptoPay ретраит. В личном кабинете можно переотправить. Наш handler идемпотентен |
| 11 | **`invoice.amount` не совпадает** | Сверяем с `payments.amount`; при несовпадении — лог `ERROR`, не зачисляем |
| 12 | **Миграция уронила схему** | Миграции идемпотентны (`IF NOT EXISTS`); бэкап обязателен |
| 13 | **Секреты в логах** | Маскируем `CRYPTO_PAY_TOKEN`, `BOT_TOKEN`, заголовок `crypto-pay-api-signature`; тело webhook на INFO не логируем |
| 14 | **`update_type ≠ invoice_paid`** | Библиотека роутит по типу; наш handler подписан только на `invoice_paid`. Прочие — 200, без действий |
| 15 | **Инвойс `status=expired` (не оплачен)** | `update_type=invoice_paid` при этом не приходит. Pending-запись остаётся — это ОК |
| 16 | **Пре-сериализация тела webhook ломает подпись** | `web.post('/cryptopay/webhook', crypto.get_updates)` регистрируем напрямую, middleware не прикручиваем |

---

## Verification

**Unit-тесты (pytest + pytest-asyncio):**
- `test_extend_subscription.py` — 3 сценария: новый / истёк / активен → `paid_until` правильный.
- `test_webhook_idempotency.py` — вызываем handler дважды с одним `invoice_id`; второй вызов не меняет БД.
- `test_issue_invite_links.py` — mock-bot; 4 вызова `create_chat_invite_link` с `member_limit=1, expire_date ≈ now + 1ч`.
- `test_payload_encoding.py` — round-trip `"<tg>:<plan>"` ↔ `decode_payload`.
- (опц.) `test_signature.py` — явный вызов `crypto.check_signature` на фиксированном body и токене.

**Интеграционный прогон (testnet):**
1. `CRYPTO_PAY_NETWORK=test`, токен у @CryptoTestnetBot.
2. `docker compose up` + ngrok → setWebhook для Telegram на ngrok; в @CryptoTestnetBot → My Apps → Webhooks тот же ngrok.
3. `/start` → меню.
4. Кнопка «3 дня — 11 USDT» → ссылка на @CryptoTestnetBot.
5. Оплата тестовым USDT.
6. В БД: `payments.status='paid'`, `users.paid_until ≈ now+3d`, `status='active'`.
7. В ЛС — 4 invite-ссылки. Первая открывается → попадаем в чат; повторно — «ссылка недействительна».
8. Повторная оплата на 7 дней → `paid_until = now + 3d + 7d`.
9. Вручную `UPDATE users SET paid_until = NOW() + INTERVAL '5 minutes'`, временно `kick_expired.interval = 1 мин` → через ~5 мин вылет из 4 чатов, `status='expired'`.
10. Аналогично с `24h 5m` и `remind_24h` → приходит напоминание.

**Smoke-чеклист перед релизом:**
- [ ] Все 4 `CHAT_IDS` корректны, бот — админ с `can_invite_users` и `can_restrict_members`.
- [ ] `getWebhookInfo` показывает правильный Telegram-URL.
- [ ] В @CryptoBot → My Apps → Webhook URL совпадает с `CRYPTO_PAY_WEBHOOK_URL`.
- [ ] `CRYPTO_PAY_NETWORK=main` (не `test`) на продакшне.
- [ ] `\d users`, `\d payments` — таблицы и индексы на месте.
- [ ] HTTPS работает без `-k`.
- [ ] `CRYPTO_PAY_TOKEN`, `BOT_TOKEN`, `crypto-pay-api-signature` не появляются в логах.

---

## Критические файлы

| Файл | Роль |
|------|------|
| `app/main.py` | точка входа; собирает app, dp, scheduler, AioCryptoPay |
| `app/config.py` | все секреты и `CHAT_IDS`; никаких хардкодов |
| `app/db/queries.py` | **единственное** место с SQL |
| `app/payments/cryptopay.py` | обёртка над aiocryptopay: клиент, `create_invoice_for`, payload-encoding |
| `app/payments/webhook.py` | `@crypto.pay_handler()` + оркестрация |
| `app/chats/manager.py` | всё, что трогает чаты через bot API |
| `app/scheduler/jobs.py` | `remind_24h`, `kick_expired`, `setup_scheduler` |
| `app/db/migrations.sql` | источник правды для схемы |
