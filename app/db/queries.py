from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from app.db.pool import get_pool


@dataclass
class ReduceResult:
    """Итог reduce_subscription. active_granted — список пар (chat_id, invite_link)
    для немедленного кика, если подписка ушла в прошлое."""
    found: bool
    new_paid_until: datetime | None = None
    now_expired: bool = False
    active_granted: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class ImportLegacyResult:
    """Итог import_legacy_user (см. granted_access source='legacy_import')."""
    new_paid_until: datetime
    was_created: bool          # был ли создан новый юзер
    granted_count: int         # сколько granted_access создано/обновлено


async def upsert_user(telegram_id: int, username: str | None) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO users (telegram_id, username)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username
        """,
        telegram_id, username,
    )


async def get_user_by_tg_id(telegram_id: int) -> dict[str, Any] | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT telegram_id, username, plan, paid_until, status FROM users WHERE telegram_id = $1",
        telegram_id,
    )
    return dict(row) if row else None


async def upsert_pending_payment(
    telegram_id: int,
    plan: str,
    amount: Decimal | int | float,
    payment_id: str,
    provider: str,
    pay_url: str,
) -> str:
    """INSERT pending; при дубле (provider, payment_id) — DO UPDATE SET pay_url
    и возврат фактического pay_url. Heleket возвращает тот же uuid для того же
    order_id → без ON CONFLICT тут UniqueViolationError. Возвращаемый pay_url
    может отличаться от переданного (если в существующей записи был другой URL).
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO payments (telegram_id, plan, amount, payment_id, status, provider, pay_url)
        VALUES ($1, $2, $3, $4, 'pending', $5, $6)
        ON CONFLICT (provider, payment_id) DO UPDATE
            SET pay_url = EXCLUDED.pay_url
        RETURNING pay_url
        """,
        telegram_id, plan, Decimal(str(amount)), payment_id, provider, pay_url,
    )
    return row["pay_url"]


async def find_active_pending_payment(
    telegram_id: int,
    plan: str,
    provider: str,
    max_age_seconds: int = 3600,
) -> Optional[dict[str, Any]]:
    """Возвращает свежую pending-запись для этой пары юзер+план+провайдер,
    если она моложе max_age_seconds и у неё уже сохранён pay_url. Записи
    без pay_url (legacy до миграции) игнорируем — пусть пройдут полный цикл
    create_invoice → upsert, который пропишет pay_url через ON CONFLICT.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, payment_id, pay_url, created_at
          FROM payments
         WHERE telegram_id = $1
           AND plan = $2
           AND provider = $3
           AND status = 'pending'
           AND pay_url IS NOT NULL
           AND created_at > NOW() - make_interval(secs => $4)
         ORDER BY created_at DESC
         LIMIT 1
        """,
        telegram_id, plan, provider, max_age_seconds,
    )
    return dict(row) if row else None


async def find_expired_pending_payment(
    telegram_id: int,
    plan: str,
    provider: str,
    max_age_seconds: int = 3600,
) -> Optional[dict[str, Any]]:
    """Возвращает ИСТЁКШУЮ pending-запись (created_at <= NOW() - max_age) для
    этой пары юзер+план+провайдер. Используется в шаге 2 on_pay для refresh
    через Heleket is_refresh=True: у Heleket TTL инвойса = 1ч, после чего
    адрес мёртвый, но через is_refresh можно обновить адрес/expired_at без
    создания нового uuid.

    Верхней границы по возрасту нет — refresh применим к любому возрасту
    старше TTL. pay_url IS NOT NULL — если ссылки нет (legacy до миграции),
    refresh не выручит, идём на шаг 3 (новый createInvoice + upsert).
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, payment_id, pay_url, created_at
          FROM payments
         WHERE telegram_id = $1
           AND plan = $2
           AND provider = $3
           AND status = 'pending'
           AND pay_url IS NOT NULL
           AND created_at <= NOW() - make_interval(secs => $4)
         ORDER BY created_at DESC
         LIMIT 1
        """,
        telegram_id, plan, provider, max_age_seconds,
    )
    return dict(row) if row else None


async def mark_pending_refreshed(
    provider: str,
    payment_id: str,
    pay_url: str,
) -> None:
    """UPDATE pay_url + created_at = NOW() для существующей pending-записи.
    Вызывается ПОСЛЕ удачного refresh-вызова к провайдеру (Heleket
    is_refresh=True): pay_url формально не меняется, но обновим на всякий
    случай, а created_at сдвигаем — чтобы find_active_pending_payment
    подхватил эту запись на следующих кликах в течение нового TTL.
    """
    pool = get_pool()
    await pool.execute(
        """
        UPDATE payments
           SET pay_url = $3,
               created_at = NOW()
         WHERE provider = $1 AND payment_id = $2
        """,
        provider, payment_id, pay_url,
    )


PLAN_DAYS: dict[str, int] = {"tariff_3d": 3, "tariff_7d": 7, "tariff_30d": 30}


async def process_paid_invoice(
    provider: str,
    payment_id: str,
    webhook_amount: Decimal | float,
) -> Optional[dict[str, Any]]:
    """Атомарно помечает платёж paid и продлевает подписку.

    Возвращает dict с telegram_id / plan / paid_until для post-payment действий,
    или None если:
      - платёж неизвестен (неверный provider+payment_id),
      - уже обработан (идемпотентность),
      - amount не совпадает с записью pending.

    В рамках той же транзакции продлевает все НЕ-revoked записи в
    granted_access для этого пользователя — закрывает кейс продления для
    юзера, который уже сидит в каналах с прошлой подписки.
    """
    if not isinstance(webhook_amount, Decimal):
        webhook_amount = Decimal(str(webhook_amount))

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT telegram_id, plan, amount, status
                  FROM payments
                 WHERE provider = $1 AND payment_id = $2
                 FOR UPDATE
                """,
                provider, payment_id,
            )
            if row is None:
                return None
            if row["status"] == "paid":
                return None
            if webhook_amount != row["amount"]:
                return None

            days = PLAN_DAYS[row["plan"]]
            await conn.execute(
                """
                UPDATE payments
                   SET status = 'paid'
                 WHERE provider = $1 AND payment_id = $2
                """,
                provider, payment_id,
            )
            updated = await conn.fetchrow(
                """
                UPDATE users
                   SET plan = $2,
                       paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                                  + make_interval(days => $3),
                       status = 'active'
                 WHERE telegram_id = $1
             RETURNING paid_until
                """,
                row["telegram_id"], row["plan"], days,
            )

            new_paid_until = updated["paid_until"] if updated else None

            # Продлеваем доступ во всех каналах, где юзер уже сидит (с прошлой
            # подписки). Если он не вступал ни разу — joined_at IS NULL,
            # запись не тронем (она протухнет естественно через 1ч expire
            # ссылки в Telegram).
            if new_paid_until is not None:
                await conn.execute(
                    """
                    UPDATE granted_access
                       SET paid_until = $2
                     WHERE telegram_id = $1
                       AND joined_at IS NOT NULL
                       AND revoked_at IS NULL
                    """,
                    row["telegram_id"], new_paid_until,
                )

            return {
                "telegram_id": row["telegram_id"],
                "plan": row["plan"],
                "paid_until": new_paid_until,
            }


async def list_expiring_between(
    start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    """Возвращает (telegram_id, paid_until) для активных подписчиков, чей
    paid_until попадает в окно [start; end]. paid_until нужен в reminder'е,
    чтобы показать юзеру реальную дату — не пересказывать тариф."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT telegram_id, paid_until
          FROM users
         WHERE status = 'active'
           AND paid_until BETWEEN $1 AND $2
        """,
        start, end,
    )
    return [dict(r) for r in rows]


async def expire_and_return_ids() -> list[int]:
    """Помечает users.status='expired' для всех просрочившихся.
    Возвращаемый список используется только в legacy kick-сценарии;
    новый kick через granted_access (см. list_expired_granted)."""
    pool = get_pool()
    now = datetime.now(timezone.utc)
    rows = await pool.fetch(
        """
        UPDATE users
           SET status = 'expired'
         WHERE status = 'active' AND paid_until < $1
        RETURNING telegram_id
        """,
        now,
    )
    return [r["telegram_id"] for r in rows]


# -----------------------------------------------------------------------------
# granted_access: per-user-per-chat access tracking
# -----------------------------------------------------------------------------

async def grant_access(
    telegram_id: int,
    chat_id: int,
    invite_link: str,
    paid_until: datetime,
) -> None:
    """Регистрирует привязку invite-ссылки к покупателю. Вызывается сразу после
    bot.create_chat_invite_link(creates_join_request=True). ON CONFLICT
    (invite_link) DO NOTHING — invite_link уникален в Telegram, дубликатов
    быть не должно, но защищаемся от ретраев."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO granted_access (telegram_id, chat_id, invite_link, paid_until)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (invite_link) DO NOTHING
        """,
        telegram_id, chat_id, invite_link, paid_until,
    )


async def find_granted_by_link(invite_link: str) -> dict[str, Any] | None:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, telegram_id, chat_id, invite_link, paid_until,
               joined_at, revoked_at
          FROM granted_access
         WHERE invite_link = $1
        """,
        invite_link,
    )
    return dict(row) if row else None


async def mark_joined(invite_link: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE granted_access SET joined_at = NOW() WHERE invite_link = $1",
        invite_link,
    )


async def list_expired_granted(limit: int = 100) -> list[dict[str, Any]]:
    """Возвращает batch granted_access записей готовых к kick.
    Кикаем:
      * source='legacy_import' (импортированные админом — joined_at у них
        выставляется автоматически, но семантически смысл другой);
      * source='invite_link' AND joined_at IS NOT NULL (юзер реально кликал
        и вступил по нашей ссылке).
    Не-joined invite_link записи пропускаем — там нет физического участника."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, telegram_id, chat_id, invite_link, source
          FROM granted_access
         WHERE paid_until < NOW()
           AND revoked_at IS NULL
           AND (
             source = 'legacy_import'
             OR (source = 'invite_link' AND joined_at IS NOT NULL)
           )
         ORDER BY paid_until ASC
         LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_revoked(granted_access_id: int) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE granted_access SET revoked_at = NOW() WHERE id = $1",
        granted_access_id,
    )


async def mark_revoked_by_link(invite_link: str) -> None:
    """Аналог mark_revoked, но по invite_link — удобно когда у нас на руках
    результат reduce_subscription (там id мы не таскаем)."""
    pool = get_pool()
    await pool.execute(
        "UPDATE granted_access SET revoked_at = NOW() WHERE invite_link = $1",
        invite_link,
    )


# -----------------------------------------------------------------------------
# reduce: уменьшение срока (зеркало extend, но с возможным мгновенным expired)
# -----------------------------------------------------------------------------

async def reduce_subscription(telegram_id: int, days: int) -> ReduceResult:
    """Атомарно уменьшает paid_until на N дней. В отличие от extend — НЕ создаёт
    юзера если его нет.

    Если новый paid_until ≤ NOW():
      * users.status='expired'
      * granted_access.paid_until синхронизируется (но НЕ revoked здесь —
        кик делает caller, чтобы залогировать каждое действие и пройти по
        Telegram API без блокировки транзакции)
      * active_granted в результате содержит (chat_id, invite_link) для
        немедленного кика.

    Если paid_until остаётся в будущем:
      * статус не трогаем
      * granted_access.paid_until синхронизируется
      * active_granted = [] (кикать не нужно).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                   SET paid_until = paid_until - make_interval(days => $2)
                 WHERE telegram_id = $1
                   AND paid_until IS NOT NULL
                RETURNING paid_until
                """,
                telegram_id, days,
            )
            if row is None:
                return ReduceResult(found=False)

            new_paid_until: datetime = row["paid_until"]
            now = datetime.now(timezone.utc)
            now_expired = new_paid_until <= now

            # Синхронизируем активные granted_access записи. Берём ВСЕ
            # active (revoked_at IS NULL), вне зависимости от joined_at —
            # для not-joined записей это just-in-case (они и так не дают
            # доступа), но не ломает ничего.
            await conn.execute(
                """
                UPDATE granted_access
                   SET paid_until = $2
                 WHERE telegram_id = $1
                   AND revoked_at IS NULL
                """,
                telegram_id, new_paid_until,
            )

            active_granted: list[tuple[int, str]] = []
            if now_expired:
                await conn.execute(
                    "UPDATE users SET status='expired' WHERE telegram_id = $1",
                    telegram_id,
                )
                # Для немедленного кика берём только тех, кто реально вступил.
                # Записи с joined_at IS NULL — это либо протухшие ссылки,
                # либо ещё не использованные; кикать там некого.
                rows = await conn.fetch(
                    """
                    SELECT chat_id, invite_link
                      FROM granted_access
                     WHERE telegram_id = $1
                       AND joined_at IS NOT NULL
                       AND revoked_at IS NULL
                    """,
                    telegram_id,
                )
                active_granted = [(r["chat_id"], r["invite_link"]) for r in rows]

            return ReduceResult(
                found=True,
                new_paid_until=new_paid_until,
                now_expired=now_expired,
                active_granted=active_granted,
            )


# -----------------------------------------------------------------------------
# legacy / halyavshchik detection
# -----------------------------------------------------------------------------

async def count_legacy_active_users() -> int:
    """Активные подписчики БЕЗ единой active записи в granted_access с joined.
    После /import_legacy для юзера появляется legacy_import-запись с
    joined_at = NOW() → счётчик уменьшается. После /reissue (revoke + новые
    invite-ссылки) — пока юзер не вступит по новой ссылке, он опять считается
    «без активного знакомства». Метрика для /health → требует /bulk_import."""
    pool = get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM users u
         WHERE u.status = 'active'
           AND u.paid_until > NOW()
           AND NOT EXISTS (
                SELECT 1 FROM granted_access ga
                 WHERE ga.telegram_id = u.telegram_id
                   AND ga.joined_at IS NOT NULL
                   AND ga.revoked_at IS NULL
           )
        """
    )
    return int(val or 0)


async def is_legacy_active_user(telegram_id: int) -> bool:
    """Тот же критерий, что в count_legacy_active_users, но для одного юзера.
    Для отображения флага в /find."""
    pool = get_pool()
    val = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM users u
             WHERE u.telegram_id = $1
               AND u.status = 'active'
               AND u.paid_until > NOW()
               AND NOT EXISTS (
                    SELECT 1 FROM granted_access ga
                     WHERE ga.telegram_id = u.telegram_id
                       AND ga.joined_at IS NOT NULL
                       AND ga.revoked_at IS NULL
               )
        )
        """,
        telegram_id,
    )
    return bool(val)


# -----------------------------------------------------------------------------
# legacy import + grant/reissue helpers
# -----------------------------------------------------------------------------

async def import_legacy_user(
    telegram_id: int,
    days: int,
    chat_ids: list[int],
) -> ImportLegacyResult:
    """Импортирует «фактического» legacy-юзера: создаёт/продлевает users +
    создаёт/обновляет granted_access записи (source='legacy_import',
    invite_link=NULL, joined_at=NOW()) для каждого chat_id.

    Семантика joined_at для legacy_import: «админ подтверждает, что юзер
    физически в чате». Это позволяет kick_expired подобрать запись.

    Возвращает (new_paid_until, was_created, granted_count). Транзакция."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT 1 FROM users WHERE telegram_id = $1", telegram_id,
            )
            if existing is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (telegram_id, status, paid_until)
                    VALUES (
                        $1, 'active',
                        NOW() + make_interval(days => $2)
                    )
                    RETURNING paid_until
                    """,
                    telegram_id, days,
                )
                was_created = True
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE users
                       SET paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                                      + make_interval(days => $2),
                           status = 'active'
                     WHERE telegram_id = $1
                    RETURNING paid_until
                    """,
                    telegram_id, days,
                )
                was_created = False
            new_paid_until: datetime = row["paid_until"]

            granted_count = 0
            for chat_id in chat_ids:
                # Если для (telegram_id, chat_id) есть active запись —
                # обновляем её paid_until. Иначе INSERT новой legacy-записи.
                upd = await conn.execute(
                    """
                    UPDATE granted_access
                       SET paid_until = $3
                     WHERE telegram_id = $1
                       AND chat_id = $2
                       AND revoked_at IS NULL
                    """,
                    telegram_id, chat_id, new_paid_until,
                )
                # asyncpg .execute() returns "UPDATE N"
                try:
                    affected = int(upd.split()[-1]) if upd else 0
                except (ValueError, AttributeError):
                    affected = 0
                if affected == 0:
                    await conn.execute(
                        """
                        INSERT INTO granted_access
                            (telegram_id, chat_id, invite_link,
                             paid_until, joined_at, source)
                        VALUES ($1, $2, NULL, $3, NOW(), 'legacy_import')
                        """,
                        telegram_id, chat_id, new_paid_until,
                    )
                granted_count += 1

            return ImportLegacyResult(
                new_paid_until=new_paid_until,
                was_created=was_created,
                granted_count=granted_count,
            )


async def count_active_breakdown() -> dict[str, int]:
    """Разбивка active-юзеров по типу присутствия в чатах. Используется в
    /health для метрики «legacy без импорта»."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        WITH per_user AS (
            SELECT
                u.telegram_id,
                EXISTS (
                    SELECT 1 FROM granted_access ga
                     WHERE ga.telegram_id = u.telegram_id
                       AND ga.source = 'invite_link'
                       AND ga.joined_at IS NOT NULL
                       AND ga.revoked_at IS NULL
                ) AS has_invite,
                EXISTS (
                    SELECT 1 FROM granted_access ga
                     WHERE ga.telegram_id = u.telegram_id
                       AND ga.source = 'legacy_import'
                       AND ga.revoked_at IS NULL
                ) AS has_legacy
              FROM users u
             WHERE u.status = 'active'
               AND u.paid_until > NOW()
        )
        SELECT
            COUNT(*) AS active_users,
            COUNT(*) FILTER (WHERE has_invite) AS via_invite,
            COUNT(*) FILTER (WHERE has_legacy) AS via_legacy,
            COUNT(*) FILTER (WHERE NOT has_invite AND NOT has_legacy)
                AS legacy_no_import
          FROM per_user
        """
    )
    return {
        "active_users": int(row["active_users"] or 0),
        "via_invite": int(row["via_invite"] or 0),
        "via_legacy": int(row["via_legacy"] or 0),
        "legacy_no_import": int(row["legacy_no_import"] or 0),
    }


async def count_user_access_breakdown(telegram_id: int) -> dict[str, int]:
    """K (invite, joined) и L (legacy_import) — для /find."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE source = 'invite_link'
                  AND joined_at IS NOT NULL
                  AND revoked_at IS NULL
            ) AS via_invite,
            COUNT(*) FILTER (
                WHERE source = 'legacy_import'
                  AND revoked_at IS NULL
            ) AS via_legacy
          FROM granted_access
         WHERE telegram_id = $1
        """,
        telegram_id,
    )
    return {
        "via_invite": int(row["via_invite"] or 0),
        "via_legacy": int(row["via_legacy"] or 0),
    }


async def get_user_paid_until(telegram_id: int) -> datetime | None:
    """SELECT paid_until — для проверки в /reissue."""
    pool = get_pool()
    return await pool.fetchval(
        "SELECT paid_until FROM users WHERE telegram_id = $1", telegram_id,
    )


async def revoke_all_active_for_user(telegram_id: int) -> int:
    """UPDATE granted_access SET revoked_at = NOW() для всех active записей
    юзера. Возвращает количество затронутых строк. Используется в /reissue
    перед выдачей новых ссылок (без kick из чата — это отдельное действие)."""
    pool = get_pool()
    result = await pool.execute(
        """
        UPDATE granted_access
           SET revoked_at = NOW()
         WHERE telegram_id = $1 AND revoked_at IS NULL
        """,
        telegram_id,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, AttributeError):
        return 0


# -----------------------------------------------------------------------------
# user-initiated reissue (кнопка «Получить ссылки заново» в /my)
# -----------------------------------------------------------------------------

async def get_reissue_status(
    telegram_id: int,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Возвращает (paid_until, last_reissue_at) для юзера или (None, None)
    если юзера нет в БД. Используется в callback `user:reissue_links` для
    проверки активности подписки и rate-limit."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT paid_until, last_reissue_at FROM users WHERE telegram_id = $1",
        telegram_id,
    )
    if row is None:
        return None, None
    return row["paid_until"], row["last_reissue_at"]


async def update_last_reissue(telegram_id: int) -> None:
    """UPDATE users SET last_reissue_at = NOW(). Отдельный helper —
    используется в perform_user_reissue_atomic, но оставлен публичным
    на случай тестов / админских манипуляций."""
    pool = get_pool()
    await pool.execute(
        "UPDATE users SET last_reissue_at = NOW() WHERE telegram_id = $1",
        telegram_id,
    )


async def perform_user_reissue_atomic(telegram_id: int) -> int:
    """Атомарно: revoke всех active granted_access + UPDATE users.last_reissue_at.
    Возвращает количество revoked записей. Делается в одной транзакции, чтобы
    не получить «обнулили last_reissue_at, но не успели revoke» (или наоборот).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE granted_access
                   SET revoked_at = NOW()
                 WHERE telegram_id = $1 AND revoked_at IS NULL
                """,
                telegram_id,
            )
            await conn.execute(
                "UPDATE users SET last_reissue_at = NOW() WHERE telegram_id = $1",
                telegram_id,
            )
            try:
                return int(result.split()[-1]) if result else 0
            except (ValueError, AttributeError):
                return 0
