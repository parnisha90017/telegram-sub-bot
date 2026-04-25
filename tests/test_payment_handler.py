"""Reuse-pending-invoice flow в on_pay (повторный клик на тариф).

Регрессия прода: при повторном клике с тем же (tg, plan) Heleket возвращает
тот же uuid → INSERT падал с UniqueViolationError, юзер залипал на «Загрузка».

Теперь:
  * перед обращением к провайдеру ищем свежий pending (≤1ч) с pay_url;
    если есть — отдаём тот же URL, провайдер не дёргается;
  * upsert с ON CONFLICT (provider, payment_id) DO UPDATE — закрывает кейс
    «провайдер вернул тот же uuid» и race;
  * find_active_pending_payment игнорирует записи с pay_url IS NULL
    (legacy до миграции) — для них пройдёт полный цикл и upsert проставит URL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.payment import on_pay
from app.payments.base import Invoice


def _mock_callback_query(data: str, tg_id: int = 999) -> MagicMock:
    cq = MagicMock(spec=CallbackQuery)
    cq.data = data
    user = MagicMock()
    user.id = tg_id
    user.username = "u"
    cq.from_user = user
    cq.message = MagicMock(spec=Message)
    cq.message.edit_text = AsyncMock()
    cq.message.answer = AsyncMock()
    cq.answer = AsyncMock()
    return cq


def _make_provider(invoice_id: str, pay_url: str) -> MagicMock:
    p = MagicMock()
    p.create_invoice = AsyncMock(return_value=Invoice(
        provider="heleket",
        invoice_id=invoice_id,
        pay_url=pay_url,
        amount_usd=21.0,
    ))
    return p


# -----------------------------------------------------------------------------
# 1) happy path: pending в БД нет — создаём новый
# -----------------------------------------------------------------------------

async def test_on_pay_creates_new_invoice():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-new-1", "https://pay.heleket/new")
    providers = {"heleket": heleket}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ) as m_find, patch(
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/new"),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_find.assert_awaited_once()
    heleket.create_invoice.assert_awaited_once()
    m_upsert.assert_awaited_once()
    _, kwargs = m_upsert.await_args
    assert kwargs["payment_id"] == "hel-new-1"
    assert kwargs["pay_url"] == "https://pay.heleket/new"

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    assert "https://pay.heleket/new" in args[0]


# -----------------------------------------------------------------------------
# 2) reuse: уже есть свежий pending с pay_url — провайдер не дёргается
# -----------------------------------------------------------------------------

async def test_on_pay_reuses_pending_invoice():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-should-not-be-called", "https://nope")
    providers = {"heleket": heleket}

    existing = {
        "id": 11,
        "payment_id": "hel-existing-1",
        "pay_url": "https://pay.heleket/existing",
        "created_at": datetime.now(timezone.utc),
    }

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=existing),
    ) as m_find, patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_find.assert_awaited_once()
    # Провайдер НЕ вызывался — это главное.
    heleket.create_invoice.assert_not_awaited()
    # Upsert тоже не зовём — запись уже есть, ничего обновлять не надо.
    m_upsert.assert_not_awaited()

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    assert "https://pay.heleket/existing" in args[0]


# -----------------------------------------------------------------------------
# 3) Heleket вернул payment_id, который уже в БД (race / legacy)
#    find ничего не нашёл (pay_url IS NULL у старой записи), идём в провайдер,
#    upsert через ON CONFLICT возвращает фактический pay_url. Не падаем.
# -----------------------------------------------------------------------------

async def test_on_pay_handles_provider_returns_existing_payment_id():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    # Heleket вернул тот же uuid что был в БД (legacy pending без pay_url)
    heleket = _make_provider("hel-collision-uuid", "https://pay.heleket/fresh")
    providers = {"heleket": heleket}

    # find игнорирует legacy-запись (pay_url IS NULL) → возвращает None
    # upsert через ON CONFLICT DO UPDATE прописывает pay_url в существующую
    # запись; RETURNING отдаёт ровно то, что только что записали.
    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        # Legacy-запись (pay_url IS NULL) → find_expired её игнорирует тоже,
        # рефреш не делаем, идём в шаг 4 (новый createInvoice + upsert).
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/fresh"),
    ) as m_upsert:
        await on_pay(cq, providers)

    # is_refresh не передавался — это не Heleket-refresh, а обычный create.
    _, ck = heleket.create_invoice.await_args
    assert ck.get("is_refresh") is None or ck.get("is_refresh") is False
    heleket.create_invoice.assert_awaited_once()
    m_upsert.assert_awaited_once()
    _, kwargs = m_upsert.await_args
    assert kwargs["payment_id"] == "hel-collision-uuid"
    assert kwargs["pay_url"] == "https://pay.heleket/fresh"

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    # Юзер видит URL — не «Загрузка».
    assert "https://pay.heleket/fresh" in args[0]


# -----------------------------------------------------------------------------
# 4) upsert_pending_payment идемпотентен: два вызова с одной (provider, payment_id)
#    второй возвращает pay_url первого (или новый, если переписали) и не падает.
#    SQL-уровень: ON CONFLICT DO UPDATE SET pay_url = EXCLUDED.pay_url RETURNING.
# -----------------------------------------------------------------------------

async def test_upsert_pending_payment_handles_conflict():
    """Мокаем asyncpg-pool. Первый вызов вставляет, второй — попадает в
    DO UPDATE и возвращает обновлённый pay_url. Никаких исключений."""
    from app.db.queries import upsert_pending_payment

    pool = MagicMock()
    # При INSERT возвращаем то, что прилетело в EXCLUDED (имитация RETURNING).
    pool.fetchrow = AsyncMock(side_effect=[
        {"pay_url": "https://pay.heleket/u1"},
        {"pay_url": "https://pay.heleket/u2"},
    ])

    with patch("app.db.queries.get_pool", return_value=pool):
        url1 = await upsert_pending_payment(
            telegram_id=42, plan="tariff_7d", amount=21,
            payment_id="hel-same", provider="heleket",
            pay_url="https://pay.heleket/u1",
        )
        url2 = await upsert_pending_payment(
            telegram_id=42, plan="tariff_7d", amount=21,
            payment_id="hel-same", provider="heleket",
            pay_url="https://pay.heleket/u2",
        )

    assert url1 == "https://pay.heleket/u1"
    assert url2 == "https://pay.heleket/u2"
    assert pool.fetchrow.await_count == 2

    # SQL содержит ON CONFLICT DO UPDATE и RETURNING — это и есть гарантия.
    sql = pool.fetchrow.await_args_list[0].args[0]
    assert "ON CONFLICT" in sql
    assert "(provider, payment_id)" in sql
    assert "DO UPDATE" in sql
    assert "RETURNING pay_url" in sql


# -----------------------------------------------------------------------------
# 5) find_active_pending_payment SQL-проверка: фильтр pay_url IS NOT NULL
#    + max_age параметр (не зашитый литерал).
# -----------------------------------------------------------------------------

async def test_find_active_pending_payment_sql_filters_correctly():
    """Регрессия: SELECT должен иметь pay_url IS NOT NULL (legacy-записи без
    URL пропускаем) и принимать max_age_seconds параметром."""
    from app.db.queries import find_active_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with patch("app.db.queries.get_pool", return_value=pool):
        await find_active_pending_payment(
            telegram_id=42, plan="tariff_7d", provider="heleket",
            max_age_seconds=1800,
        )

    sql = pool.fetchrow.await_args.args[0]
    args = pool.fetchrow.await_args.args
    assert "pay_url IS NOT NULL" in sql
    assert "status = 'pending'" in sql
    # max_age пробрасывается параметром, не литералом.
    assert 1800 in args
    assert "make_interval" in sql


# -----------------------------------------------------------------------------
# 6) Heleket-refresh: истёкший pending → create_invoice(is_refresh=True),
#    юзер видит ТОТ ЖЕ pay_url (uuid не плодим), provider дёргается ровно
#    один раз с is_refresh=True.
# -----------------------------------------------------------------------------

async def test_on_pay_refreshes_expired_heleket_invoice():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    # Heleket для is_refresh=True вернёт ТОТ ЖЕ uuid и url, только
    # address+expired_at обновятся (на нашей стороне — невидимо).
    heleket = _make_provider("hel-uuid-stable", "https://pay.heleket/stable")
    providers = {"heleket": heleket}

    expired = {
        "id": 33,
        "payment_id": "hel-uuid-stable",
        "pay_url": "https://pay.heleket/stable",
        "created_at": datetime.now(timezone.utc),  # точное значение неважно
    }

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value=expired),
    ) as m_find_expired, patch(
        "app.bot.handlers.payment.mark_pending_refreshed", new=AsyncMock(),
    ) as m_mark, patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_find_expired.assert_awaited_once()
    # provider вызван один раз — именно с is_refresh=True
    heleket.create_invoice.assert_awaited_once()
    _, kwargs = heleket.create_invoice.await_args
    assert kwargs.get("is_refresh") is True
    # БД обновлена: pay_url + created_at пересохранены
    m_mark.assert_awaited_once()
    _, mk = m_mark.await_args
    assert mk["provider"] == "heleket"
    assert mk["payment_id"] == "hel-uuid-stable"
    assert mk["pay_url"] == "https://pay.heleket/stable"
    # Шаг 4 (новый инвойс + upsert) НЕ выполнялся — мы вышли по return.
    m_upsert.assert_not_awaited()
    # Юзер видит тот же URL.
    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    assert "https://pay.heleket/stable" in args[0]


async def test_on_pay_refresh_uses_db_payment_id_not_provider_response(caplog):
    """Edge case: если Heleket-API в будущем начнёт возвращать ДРУГОЙ uuid
    при is_refresh=True (что противоречит текущей доке), мы ДОЛЖНЫ:
      1. UPDATE'ить в БД именно ту запись, которую только что зарефрешили
         (по СТАРОМУ uuid из expired) — иначе UPDATE 0 строк, мёртвый
         pay_url остаётся в БД, юзер на след. клике опять получит refresh-loop.
      2. Залогировать WARNING про расхождение — это сигнал, что доку обновили
         и стратегия требует пересмотра."""
    import logging

    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    # Провайдер вернул другой uuid (на текущий момент по доке быть не должно)
    heleket = _make_provider("hel-NEW-uuid", "https://pay.heleket/refreshed")
    providers = {"heleket": heleket}

    expired = {
        "id": 33,
        "payment_id": "hel-OLD-uuid",
        "pay_url": "https://pay.heleket/old-mortified",
        "created_at": datetime.now(timezone.utc),
    }

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value=expired),
    ), patch(
        "app.bot.handlers.payment.mark_pending_refreshed", new=AsyncMock(),
    ) as m_mark, patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ), caplog.at_level(logging.WARNING, logger="app.bot.handlers.payment"):
        await on_pay(cq, providers)

    # 1. mark_pending_refreshed получил СТАРЫЙ uuid (из БД), не из ответа.
    m_mark.assert_awaited_once()
    _, mk = m_mark.await_args
    assert mk["payment_id"] == "hel-OLD-uuid", (
        f"должен использоваться uuid из БД (expired['payment_id']), "
        f"чтобы UPDATE нашёл строку; got {mk['payment_id']!r}"
    )
    # pay_url берём из ответа — он мог обновиться (страховка).
    assert mk["pay_url"] == "https://pay.heleket/refreshed"

    # 2. WARNING про расхождение залогирован.
    assert any(
        "different uuid" in r.message and "old=hel-OLD-uuid" in r.message
        and "new=hel-NEW-uuid" in r.message
        for r in caplog.records if r.levelname == "WARNING"
    ), f"ожидаем WARNING про расхождение uuid, got: {[r.message for r in caplog.records]}"


# -----------------------------------------------------------------------------
# 7) CryptoBot: refresh-ветку ПРОПУСКАЕМ даже если есть истёкший pending.
#    CryptoBot про is_refresh не знает — для него идём на шаг 4 (новый инвойс).
# -----------------------------------------------------------------------------

async def test_on_pay_does_not_refresh_for_cryptobot():
    cq = _mock_callback_query("pay:tariff_7d:cryptobot", tg_id=42)
    cryptobot = _make_provider("cb-new-uuid", "https://t.me/CryptoBot?start=cb-new-uuid")
    providers = {"cryptobot": cryptobot}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        # Даже если в БД лежит истёкшая запись — для CryptoBot её
        # игнорируем; функция вообще не должна вызваться.
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value={"payment_id": "ignored", "pay_url": "ignored"}),
    ) as m_find_expired, patch(
        "app.bot.handlers.payment.mark_pending_refreshed", new=AsyncMock(),
    ) as m_mark, patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://t.me/CryptoBot?start=cb-new-uuid"),
    ) as m_upsert:
        await on_pay(cq, providers)

    # Для CryptoBot refresh-ветку не входим вовсе.
    m_find_expired.assert_not_awaited()
    m_mark.assert_not_awaited()
    # provider дёрнут БЕЗ is_refresh.
    cryptobot.create_invoice.assert_awaited_once()
    _, ck = cryptobot.create_invoice.await_args
    assert "is_refresh" not in ck
    # Шаг 4 отработал.
    m_upsert.assert_awaited_once()
    cq.message.edit_text.assert_awaited_once()


# -----------------------------------------------------------------------------
# 8) HeleketProvider.create_invoice — is_refresh: True реально попадает в HTTP body
# -----------------------------------------------------------------------------

async def test_create_invoice_passes_is_refresh_to_heleket_api():
    """Провайдер должен подложить "is_refresh": true в body запроса (и
    подпись остаётся консистентной — sign считается от того же body_bytes,
    которые улетают в data=). Без is_refresh — поля не быть."""
    from aioresponses import aioresponses

    from app.payments.heleket import HELEKET_API_URL, HeleketProvider

    provider = HeleketProvider(
        merchant_uuid="m-uuid",
        api_key="secret",
        callback_url="https://bot/cb",
    )

    # Случай 1: is_refresh=True → "is_refresh": true в теле.
    with aioresponses() as mocked:
        mocked.post(HELEKET_API_URL, payload={
            "state": 0,
            "result": {"uuid": "hel-uuid", "url": "https://pay.heleket/x"},
        })
        await provider.create_invoice(
            amount_usd=21.0,
            order_id="42_tariff_7d",
            is_refresh=True,
        )
        # Берём последний request — body передаётся как bytes в `data`.
        req = list(mocked.requests.values())[-1][-1]
        sent_body = req.kwargs["data"]
        assert b'"is_refresh":true' in sent_body, (
            f"is_refresh:true должен быть в body, got: {sent_body!r}"
        )

    # Случай 2: is_refresh не передан → поля в теле НЕТ.
    with aioresponses() as mocked2:
        mocked2.post(HELEKET_API_URL, payload={
            "state": 0,
            "result": {"uuid": "hel-uuid-2", "url": "https://pay.heleket/y"},
        })
        await provider.create_invoice(
            amount_usd=21.0,
            order_id="42_tariff_7d",
        )
        req = list(mocked2.requests.values())[-1][-1]
        sent_body = req.kwargs["data"]
        assert b"is_refresh" not in sent_body, (
            f"is_refresh должен отсутствовать когда не передан, got: {sent_body!r}"
        )


# -----------------------------------------------------------------------------
# 9) find_expired_pending_payment SQL: created_at <= NOW() - max_age,
#    pay_url IS NOT NULL, max_age параметром.
# -----------------------------------------------------------------------------

async def test_find_expired_pending_payment_returns_only_old_records():
    """Регрессия: SELECT должен фильтровать ИСТЁКШИЕ записи (>=max_age),
    игнорировать legacy без pay_url, и принимать max_age_seconds параметром.
    Это гарантирует отсутствие пересечения с find_active (тот берёт <max_age)."""
    from app.db.queries import find_expired_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with patch("app.db.queries.get_pool", return_value=pool):
        await find_expired_pending_payment(
            telegram_id=42, plan="tariff_7d", provider="heleket",
            max_age_seconds=3600,
        )

    sql = pool.fetchrow.await_args.args[0]
    args = pool.fetchrow.await_args.args
    # ВАЖНО: <= для истёкших (без перехлёста с find_active, у которого >).
    assert "created_at <= NOW() - make_interval(secs => $4)" in sql
    # Никаких find_active'овских "created_at >" в этом SELECT'е быть не должно.
    assert "created_at >" not in sql
    # Legacy без URL — не наш кейс для refresh.
    assert "pay_url IS NOT NULL" in sql
    # status = 'pending' — refresh применим только к pending'ам.
    assert "status = 'pending'" in sql
    # max_age не зашит литералом.
    assert 3600 in args


# -----------------------------------------------------------------------------
# 10) mark_pending_refreshed: SET pay_url AND created_at = NOW().
#     КРИТИЧНО: без UPDATE created_at следующий клик через 5 мин снова
#     попадёт в find_expired (created_at старый) → ненужный refresh-loop в
#     Heleket. created_at = NOW() гарантирует, что зайдём через find_active.
# -----------------------------------------------------------------------------

async def test_mark_pending_refreshed_updates_created_at_and_pay_url():
    """Без живой БД проверяем сам SQL: UPDATE содержит обе колонки,
    WHERE — provider + payment_id (никаких ID или telegram_id)."""
    from app.db.queries import mark_pending_refreshed

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.db.queries.get_pool", return_value=pool):
        await mark_pending_refreshed(
            provider="heleket",
            payment_id="hel-uuid",
            pay_url="https://pay.heleket/refreshed",
        )

    pool.execute.assert_awaited_once()
    sql = pool.execute.await_args.args[0]
    args = pool.execute.await_args.args[1:]

    # КРИТИЧНО: created_at = NOW() — иначе следующий клик попадёт опять
    # в find_expired и пойдёт ненужный refresh.
    assert "created_at = NOW()" in sql, (
        "mark_pending_refreshed обязан сбросить created_at — иначе "
        "find_active не подхватит запись и refresh пойдёт повторно"
    )
    # pay_url тоже обновляется (страховка на случай если Heleket изменит URL).
    assert "pay_url = $3" in sql or "pay_url=$3" in sql.replace(" ", "")
    # WHERE по provider+payment_id, иначе можем задеть чужую запись.
    assert "provider = $1" in sql
    assert "payment_id = $2" in sql
    # Параметры в правильном порядке.
    assert args == ("heleket", "hel-uuid", "https://pay.heleket/refreshed")
