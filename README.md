# telegram-sub-bot

Telegram-бот подписочной системы. Оплата USDT через **CryptoPay (@CryptoBot)**.
После оплаты — 4 одноразовые invite-ссылки в приватные чаты. За 24ч до истечения — напоминание; после истечения — kick из всех чатов.

**Стек:** Python 3.11+, aiogram 3.x, aiocryptopay, asyncpg (PostgreSQL), APScheduler, aiohttp.

Подробная архитектура: [docs/plans/architecture.md](docs/plans/architecture.md).

## Тарифы

| Тариф       | Цена     | Срок   |
|-------------|----------|--------|
| `tariff_3d` | 11 USDT  | 3 дня  |
| `tariff_7d` | 21 USDT  | 7 дней |
| `tariff_30d`| 60 USDT  | 30 дней|

Повторная оплата **суммирует** дни к текущему `paid_until`.

## Переменные окружения (.env)

См. [.env.example](.env.example). Ключевые:

- `BOT_TOKEN`, `BOT_USERNAME` — токен и юзернейм бота (без `@`).
- `CRYPTO_PAY_TOKEN` — токен от @CryptoBot (или @CryptoTestnetBot для теста).
- `CRYPTO_PAY_NETWORK` — `main` или `test`.
- `TELEGRAM_WEBHOOK_URL`, `CRYPTO_PAY_WEBHOOK_URL` — публичные HTTPS-адреса.
- `TELEGRAM_WEBHOOK_SECRET` — случайная строка (≥ 32 байта).
- `CHAT_IDS` — 4 ID приватных чатов через запятую.
- `DATABASE_URL` — строка подключения PostgreSQL.

## Запуск локально (docker compose)

```bash
cp .env.example .env   # и заполнить
docker compose up -d --build
curl http://localhost:8080/health   # ok
```

Миграции применяются автоматически при старте (`app/db/pool.py` прогоняет `app/db/migrations.sql`).

Для разработки с реальным ботом нужен публичный HTTPS — проще всего поднять `ngrok http 8080`, подставить URL в `TELEGRAM_WEBHOOK_URL` и в настройки webhook у @CryptoTestnetBot (`My Apps → Webhooks`).

## Деплой

- Один инстанс (крон не рассчитан на HA — см. [риск #9](docs/plans/architecture.md)).
- HTTPS через nginx/Caddy/Traefik как reverse proxy.
- Бот должен быть **админом** во всех 4 чатах с правами `can_invite_users` и `can_restrict_members`. При старте бот это проверяет и пишет WARN-лог, если чего-то не хватает.
- Webhook CryptoPay настраивается в @CryptoBot → `My Apps → Webhooks`.
- `CRYPTO_PAY_NETWORK=main` на продакшне (не `test`).

## Структура

```
app/
├── main.py                  # точка входа: aiohttp + aiogram + APScheduler + AioCryptoPay
├── config.py                # pydantic-settings
├── bot/
│   ├── handlers/start.py    # /start + меню
│   ├── handlers/payment.py  # callback buy:<plan> → invoice
│   ├── keyboards.py
│   └── texts.py
├── db/
│   ├── pool.py
│   ├── migrations.sql
│   └── queries.py           # всё SQL здесь
├── payments/
│   ├── cryptopay.py         # обёртка над aiocryptopay
│   └── webhook.py           # @crypto.pay_handler()
├── chats/
│   └── manager.py           # invite-ссылки, kick, предстарт-check прав
└── scheduler/
    └── jobs.py              # remind_24h, kick_expired
```

## Маршруты HTTP

| Путь                   | Назначение                                    |
|------------------------|-----------------------------------------------|
| `GET /health`          | liveness                                      |
| `POST /tg/webhook`     | Telegram Bot API (валидирует `secret_token`)  |
| `POST /cryptopay/webhook` | CryptoPay (подпись HMAC-SHA256 проверяет `aiocryptopay.get_updates`) |
