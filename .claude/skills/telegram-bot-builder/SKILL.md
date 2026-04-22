---
name: telegram-bot-builder
description: Специализированный скилл для разработки нашего Telegram-бота
  подписочной системы на Python + aiogram 3.x с крипто-оплатой через
  CryptoPay (@CryptoBot) (только USDT), управлением 4 приватными чатами
  и крон-задачами APScheduler.
risk: low
source: адаптировано под проект telegram-sub-bot
date_added: 2026-04-22
---

# Конструктор Telegram-бота подписок

Скилл описывает архитектуру, паттерны кода и правила сопровождения **нашего конкретного бота** — подписочной системы с крипто-оплатой **CryptoPay (@CryptoBot)** (USDT) и выдачей одноразовых invite-ссылок в 4 приватных чата.

**Роль:** Архитектор Telegram-бота подписок на aiogram 3.x.

## Когда применять

- Разработка команд, хендлеров, FSM для бота подписок.
- Интеграция с CryptoPay (@CryptoBot) через библиотеку `aiocryptopay` (создание инвойса, webhook, проверка подписи).
- Работа с invite-ссылками (создание на 1 использование / срок 1 час, kick просрочивших).
- Планирование и отладка крон-задач APScheduler (напоминания, авто-удаление).
- Проектирование схемы БД PostgreSQL (asyncpg).
- Деплой под webhook (не polling).

## Когда НЕ применять

- Общие вопросы Python/SQL не по этому боту.
- Рефакторинг, не связанный с подпиской/чатами/оплатой.
- Mini App, AI, Telegram Payments, аналитика пользователей — **вне скоупа** этого проекта.
- Другие платёжные провайдеры (Stripe, Cryptomus, NOWPayments) — у нас **только CryptoPay**.

---

## Архитектура проекта

### Технологический стек

| Компонент | Выбор | Назначение |
|-----------|-------|------------|
| Язык | Python 3.11+ | основной |
| Bot framework | aiogram 3.x | async-хендлеры, FSM |
| БД | PostgreSQL + asyncpg | данные пользователей и платежей |
| Планировщик | APScheduler (AsyncIOScheduler) | напоминания, авто-удаление |
| HTTP-сервер | aiohttp | Telegram webhook + CryptoPay webhook |
| Оплата | CryptoPay (@CryptoBot) через `aiocryptopay` | создание invoice, webhook (только USDT) |

### Тарифы

| Кнопка | Цена | Длительность |
|--------|------|--------------|
| Тариф 1 | 11 USDT | 3 дня |
| Тариф 2 | 21 USDT | 7 дней |
| Тариф 3 | 60 USDT | 30 дней |

Продление: при повторной оплате дни **добавляются** к текущему `paid_until`, а не обнуляются.

### User flow

```
/start
  └─ меню с 3 кнопками тарифов (inline keyboard)
      └─ выбор тарифа → create_invoice через aiocryptopay
          └─ пользователю отправляется bot_invoice_url (ссылка на @CryptoBot)
              └─ CryptoPay → webhook POST на /cryptopay/webhook
                  ├─ проверка подписи (HMAC-SHA256) — встроено в aiocryptopay
                  ├─ идемпотентность по invoice_id
                  ├─ обновление paid_until в users
                  └─ генерация 4 invite-ссылок (limit=1, expire=1h)
                      └─ отправка всех 4 ссылок пользователю в ЛС
```

Крон-задачи:
- **за 24 часа** до истечения `paid_until` — напоминание пользователю в ЛС.
- **по истечении** — `ban_chat_member` + `unban_chat_member` (kick) из всех 4 чатов, статус `expired`.

### Структура каталогов

```
telegram-sub-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                 # точка входа, запуск aiogram + aiohttp + scheduler + AioCryptoPay
│   ├── config.py               # pydantic-settings: токены, DSN, chat IDs, BOT_USERNAME
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── handlers/
│   │   │   ├── start.py        # /start + меню тарифов
│   │   │   └── payment.py      # callback на кнопку тарифа → создание invoice
│   │   ├── keyboards.py        # inline-клавиатура 3 тарифов
│   │   └── texts.py            # тексты сообщений (ru)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── pool.py             # asyncpg.Pool
│   │   ├── migrations.sql      # CREATE TABLE users, payments
│   │   └── queries.py          # все SQL-запросы
│   ├── payments/
│   │   ├── __init__.py
│   │   ├── cryptopay.py        # build_client, create_invoice_for, encode/decode_payload
│   │   └── webhook.py          # @crypto.pay_handler() — обработка invoice_paid
│   ├── chats/
│   │   └── manager.py          # create_invite_link × 4, kick_from_all_chats
│   └── scheduler/
│       └── jobs.py             # APScheduler: remind_24h, kick_expired
├── .env.example
├── requirements.txt            # aiogram>=3.4, aiocryptopay, asyncpg, aiohttp, APScheduler, pydantic-settings
└── README.md
```

---

## Схема БД

```sql
-- app/db/migrations.sql

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    plan        TEXT,                          -- 'tariff_3d' | 'tariff_7d' | 'tariff_30d'
    paid_until  TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'new',   -- 'new' | 'active' | 'expired'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    plan        TEXT NOT NULL,
    amount      NUMERIC(12, 2) NOT NULL,       -- USDT
    payment_id  TEXT NOT NULL UNIQUE,          -- CryptoPay invoice_id (строкой) — ключ идемпотентности
    status      TEXT NOT NULL,                 -- 'pending' | 'paid' | 'failed' | 'expired'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_paid_until ON users(paid_until) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_payments_telegram_id ON payments(telegram_id);
```

Уникальный `payment_id` — аппаратная защита от двойной обработки webhook'а при retry.

---

## Inline-клавиатура тарифов

```python
# app/bot/keyboards.py
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PLANS = {
    "tariff_3d":  {"title": "3 дня — 11 USDT",  "amount": 11, "days": 3},
    "tariff_7d":  {"title": "7 дней — 21 USDT", "amount": 21, "days": 7},
    "tariff_30d": {"title": "30 дней — 60 USDT","amount": 60, "days": 30},
}

def plans_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p["title"], callback_data=f"buy:{key}")]
        for key, p in PLANS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

Хендлер `/start`:

```python
# app/bot/handlers/start.py
from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.keyboards import plans_kb

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Выберите тариф подписки (оплата в USDT):",
        reply_markup=plans_kb(),
    )
```

Callback на кнопку тарифа:

```python
# app/bot/handlers/payment.py
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import PLANS
from app.payments.cryptopay import create_invoice_for
from app.db.queries import upsert_user, insert_pending_payment

router = Router()

@router.callback_query(F.data.startswith("buy:"))
async def on_buy(cq: CallbackQuery, crypto) -> None:         # crypto — AioCryptoPay, внедряется через workflow_data
    plan_key = cq.data.split(":", 1)[1]
    plan = PLANS[plan_key]

    await upsert_user(cq.from_user.id, cq.from_user.username)

    invoice_id, url = await create_invoice_for(crypto, cq.from_user.id, plan_key)

    await insert_pending_payment(
        telegram_id=cq.from_user.id,
        plan=plan_key,
        amount=plan["amount"],
        payment_id=str(invoice_id),
    )
    await cq.message.answer(
        f"Счёт на {plan['amount']} USDT создан. Оплатите по ссылке:\n{url}"
    )
    await cq.answer()
```

Экземпляр `AioCryptoPay` создаётся один раз в `main.py` и прокидывается в хендлеры через `dp['crypto']` или `dp.workflow_data.update(crypto=crypto)`.

---

## Интеграция CryptoPay (@CryptoBot) через aiocryptopay

### Протокол

- **База API:** `https://pay.crypt.bot/api/` (mainnet), `https://testnet-pay.crypt.bot/api/` (testnet).
- **Авторизация исходящих запросов:** заголовок `Crypto-Pay-API-Token: <app_token>`.
- **Подпись входящего webhook:** заголовок `crypto-pay-api-signature` = `HMAC-SHA256(key=SHA256(app_token), body_raw)`.
- **Библиотека `aiocryptopay`** скрывает оба заголовка: для исходящих — ставит `Crypto-Pay-API-Token`; для входящих — `crypto.get_updates` читает сырое тело, считает HMAC-SHA256 и сравнивает с заголовком.
- Мы **не пишем** HMAC сами.

### Создание инвойса

```python
# app/payments/cryptopay.py
from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.const import Assets, PaidButtons

from app.config import settings
from app.bot.keyboards import PLANS

def build_client() -> AioCryptoPay:
    network = Networks.MAIN_NET if settings.crypto_pay_network == "main" else Networks.TEST_NET
    return AioCryptoPay(token=settings.crypto_pay_token, network=network)

def encode_payload(telegram_id: int, plan_key: str) -> str:
    return f"{telegram_id}:{plan_key}"

def decode_payload(payload: str) -> tuple[int, str]:
    tg_id, plan_key = payload.split(":", 1)
    return int(tg_id), plan_key

async def create_invoice_for(
    crypto: AioCryptoPay, telegram_id: int, plan_key: str
) -> tuple[int, str]:
    plan = PLANS[plan_key]
    invoice = await crypto.create_invoice(
        asset=Assets.USDT,
        amount=plan["amount"],
        description=f"Подписка — {plan['title']}",
        payload=encode_payload(telegram_id, plan_key),
        expires_in=3600,
        paid_btn_name=PaidButtons.CALLBACK,
        paid_btn_url=f"https://t.me/{settings.bot_username}",
        allow_comments=False,
        allow_anonymous=False,
    )
    return invoice.invoice_id, invoice.bot_invoice_url
```

Источник истины `telegram_id` и `plan_key` — **наша запись в `payments`** (ключ = `invoice_id`). Поле `payload` в Invoice — вторичная метка для логов и дебага.

### Webhook CryptoPay

Вход — один POST на `/cryptopay/webhook`. Обрабатываем **только** `update_type="invoice_paid"`; статусы инвойса в CryptoPay — `active | paid | expired`.

```python
# app/payments/webhook.py
import logging
from decimal import Decimal

from aiocryptopay import AioCryptoPay
from aiocryptopay.models.update import Update
from aiogram import Bot

from app.chats.manager import issue_invite_links_and_send
from app.db.pool import pool

log = logging.getLogger(__name__)
PLAN_DAYS = {"tariff_3d": 3, "tariff_7d": 7, "tariff_30d": 30}

def register_cryptopay_handlers(crypto: AioCryptoPay, bot: Bot) -> None:

    @crypto.pay_handler()
    async def on_invoice_paid(update: Update, app) -> None:
        invoice = update.payload
        invoice_id = str(invoice.invoice_id)

        if invoice.status != "paid":
            return  # защитно: библиотека не должна роутить сюда с другим статусом

        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT telegram_id, plan, amount, status FROM payments "
                    "WHERE payment_id = $1 FOR UPDATE",
                    invoice_id,
                )
                if row is None:
                    log.warning("unknown invoice %s", invoice_id)
                    return
                if row["status"] == "paid":
                    return  # идемпотентность

                if Decimal(str(invoice.amount)) != row["amount"]:
                    log.error("amount mismatch on %s", invoice_id)
                    return

                await conn.execute(
                    "UPDATE payments SET status='paid' WHERE payment_id=$1",
                    invoice_id,
                )
                await conn.execute(
                    """
                    UPDATE users
                       SET plan = $2,
                           paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                                      + ($3 || ' days')::interval,
                           status = 'active'
                     WHERE telegram_id = $1
                    """,
                    row["telegram_id"], row["plan"], PLAN_DAYS[row["plan"]],
                )

        # Telegram I/O — вне транзакции:
        await issue_invite_links_and_send(bot, row["telegram_id"])
```

Регистрация маршрута — напрямую, **без middleware**, которое читает/пере-сериализует body (иначе сигнатура сломается):

```python
# в main.py
app.router.add_post("/cryptopay/webhook", crypto.get_updates)
```

---

## Управление 4 приватными чатами

ID всех 4 чатов хранятся в `settings.chat_ids: list[int]`. Бот должен быть админом с правом `can_invite_users`, а для kick — `can_restrict_members`.

### Выдача invite-ссылок после оплаты

```python
# app/chats/manager.py
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import settings

async def issue_invite_links_and_send(bot: Bot, telegram_id: int) -> None:
    expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
    links: list[str] = []
    for chat_id in settings.chat_ids:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=chat_id,
                expire_date=expire_at,
                member_limit=1,
            )
            links.append(link.invite_link)
        except Exception as e:
            # Логируем и идём дальше — юзер получит ссылки из оставшихся чатов
            print(f"invite for chat {chat_id} failed: {e}")

    text = "Оплата получена. Ваши одноразовые ссылки (действуют 1 час):\n\n" + \
           "\n".join(f"{i+1}. {url}" for i, url in enumerate(links))
    await bot.send_message(telegram_id, text, disable_web_page_preview=True)
```

### Kick просрочивших

```python
# app/chats/manager.py (продолжение)
import asyncio

async def kick_from_all_chats(bot: Bot, telegram_id: int) -> None:
    for chat_id in settings.chat_ids:
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=telegram_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=telegram_id)  # чтобы мог вернуться после новой оплаты
        except Exception as e:
            print(f"kick {telegram_id} from {chat_id} failed: {e}")
        await asyncio.sleep(0.1)  # щадим rate limit
```

---

## Крон-задачи (APScheduler)

Запускаем `AsyncIOScheduler` в том же event loop, что и бота.

```python
# app/scheduler/jobs.py
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from app.db.pool import pool
from app.chats.manager import kick_from_all_chats

async def remind_24h(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23, minutes=30)
    window_end   = now + timedelta(hours=24, minutes=30)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_id
              FROM users
             WHERE status = 'active'
               AND paid_until BETWEEN $1 AND $2
            """,
            window_start, window_end,
        )
    for r in rows:
        try:
            await bot.send_message(
                r["telegram_id"],
                "Ваша подписка истекает через 24 часа. Продлите в /start.",
            )
        except Exception:
            pass  # пользователь мог заблокировать бота

async def kick_expired(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE users
               SET status = 'expired'
             WHERE status = 'active' AND paid_until < $1
         RETURNING telegram_id
            """,
            now,
        )
    for r in rows:
        await kick_from_all_chats(bot, r["telegram_id"])

def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(remind_24h, "interval", hours=1, args=[bot], id="remind_24h")
    scheduler.add_job(kick_expired, "interval", minutes=10, args=[bot], id="kick_expired")
    return scheduler
```

Окно напоминания `[-30мин; +30мин]` к метке 24h защищает от пропусков при краткой задержке запуска; повторная отправка исключается тем, что задача идёт `hours=1`.

---

## Развёртывание под webhook

Polling **не используем** — только webhook. Поднимаем один aiohttp-сервер, который обслуживает два маршрута: Telegram и CryptoPay.

```python
# app/main.py
import logging

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.config import settings
from app.bot.handlers import start as start_h, payment as pay_h
from app.payments.cryptopay import build_client
from app.payments.webhook import register_cryptopay_handlers
from app.scheduler.jobs import setup_scheduler
from app.db.pool import init_pool, close_pool

async def on_tg_startup(bot: Bot) -> None:
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )

async def on_tg_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()

def build_app() -> web.Application:
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_routers(start_h.router, pay_h.router)
    dp.startup.register(on_tg_startup)
    dp.shutdown.register(on_tg_shutdown)

    crypto = build_client()
    dp["crypto"] = crypto                              # доступно в хендлерах как аргумент
    register_cryptopay_handlers(crypto, bot)

    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.telegram_webhook_secret,
    ).register(app, path="/tg/webhook")

    app.router.add_post("/cryptopay/webhook", crypto.get_updates)

    async def on_start(_: web.Application) -> None:
        await init_pool()
        scheduler = setup_scheduler(bot)
        scheduler.start()
        app["scheduler"] = scheduler

    async def on_stop(_: web.Application) -> None:
        await crypto.close()
        await close_pool()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    setup_application(app, dp, bot=bot)
    return app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(build_app(), host="0.0.0.0", port=settings.port)
```

Требования к хосту: публичный HTTPS (reverse-proxy — nginx/Caddy/Traefik), постоянный процесс (systemd/docker), **один инстанс** (иначе крон задвоится).

---

## Проверки качества кода

| Проверка | Severity | Что смотреть |
|----------|----------|--------------|
| `BOT_TOKEN` и `CRYPTO_PAY_TOKEN` захардкожены | HIGH | все секреты → `pydantic-settings` из `.env` |
| Webhook CryptoPay не обёрнут в `crypto.get_updates` | HIGH | маршрут `/cryptopay/webhook` регистрируется именно через `crypto.get_updates` — тогда проверка HMAC-SHA256 встроена |
| Middleware читает/парсит body до `crypto.get_updates` | HIGH | пересериализация тела ломает подпись; роут регистрируем напрямую, без middleware |
| Нет идемпотентности по `payment_id` (=`invoice_id`) | HIGH | `UNIQUE` на `payments.payment_id` + `SELECT … FOR UPDATE` + early-return при `status='paid'` |
| Invite-ссылка без `member_limit=1` или без `expire_date` | HIGH | всегда оба параметра, expire = 1 час |
| Продление обнуляет подписку вместо суммирования | HIGH | `GREATEST(COALESCE(paid_until, NOW()), NOW()) + N дней` |
| Нет сверки `invoice.amount` с `payments.amount` | MEDIUM | при несовпадении — `log.error` и НЕ зачислять |
| Крон-джоба может отправить напоминание повторно | MEDIUM | окно ±30 мин и фиксированный интервал 1 час |
| Два инстанса бота одновременно | MEDIUM | один процесс, либо APScheduler с SQLAlchemyJobStore |
| Нет обработки `TelegramForbiddenError` при рассылке напоминаний | LOW | try/except, но подписку не отменяем из-за блокировки бота |

---

## Чеклист перед деплоем

- [ ] `.env` заполнен: `BOT_TOKEN`, `BOT_USERNAME`, `DATABASE_URL`, `CRYPTO_PAY_TOKEN`, `CRYPTO_PAY_NETWORK`, `CRYPTO_PAY_WEBHOOK_URL`, `CHAT_IDS`, `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET`.
- [ ] Бот добавлен во все 4 чата админом с правами `can_invite_users` и `can_restrict_members`.
- [ ] Миграция `migrations.sql` применена; `UNIQUE(payment_id)` на месте.
- [ ] `setWebhook` на HTTPS-домен; `secret_token` проверяется.
- [ ] В @CryptoBot → Crypto Pay → My Apps → Webhooks включен и указан URL, соответствующий `/cryptopay/webhook`.
- [ ] `CRYPTO_PAY_NETWORK=main` (не `test`) на продакшне.
- [ ] Тестовый прогон в `test`: оплата у @CryptoTestnetBot → webhook → 4 ссылки → вход в чаты → kick после истечения `paid_until`.
