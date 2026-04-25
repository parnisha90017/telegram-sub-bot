WELCOME_NEW = (
    "Добро пожаловать! Это бот платной подписки.\n"
    "Оплата в USDT через @CryptoBot."
)

WELCOME_ACTIVE = "Ваша подписка активна до {paid_until}"

WELCOME_EXPIRED = (
    "Срок вашей подписки истёк.\n"
    "Продлите доступ, чтобы вернуться в чаты."
)

SELECT_PLAN = "Выберите тариф (оплата в USDT):"

SELECT_PROVIDER = "Выбери способ оплаты:"

INVOICE_CREATED = (
    "Счёт на {amount} USDT создан.\n"
    "Оплатите по ссылке (срок — 1 час):\n{url}"
)

INVOICE_ERROR = "Не удалось создать счёт. Попробуйте позже."

PAID_WITH_LINKS = (
    "Оплата получена. ✅\n"
    "Необходимо в каждом чате выполнить инструкцию в закрепленном сообщении "
    "по отдельности для получения громкого сигнала.\n\n"
    "Вот ваши ссылки для входа (действительны 1 час):\n\n{links}\n\n"
    "❗️Перепроверьте вступление во все каналы. Если сработал лимит ТГ, "
    "попробуйте вступить через пару минут.\n\n"
    "Приложение скоро вернем в работу!"
)

ABOUT_US = (
    "Первые на рынке громкие сигналы, с автоматизированной системой "
    "мониторинга рабочего метода. После оплаты, вышлем вам инструкцию "
    "по подключению. Работает по направлениям: НК ДС, НК КМ, НТ КМ, СМС-переводы."
)

REMIND_24H = (
    "⏰ Твоя подписка истекает {paid_until_str}.\n"
    "До окончания осталось примерно {hours_left} ч.\n\n"
    "Чтобы продлить — /start."
)

MY_SUBSCRIPTION_ACTIVE = (
    "📋 Твоя подписка\n\n"
    "Истекает: {paid_until_str}\n"
    "Осталось: {remaining_str}\n"
    "Статус: ✅ активна\n\n"
    "Чтобы продлить — /start"
)

MY_SUBSCRIPTION_NONE = (
    "📋 У тебя нет активной подписки.\n\n"
    "Чтобы оформить — /start"
)

ADMIN_PANEL_TEXT = (
    "🔧 <b>Админ-панель</b>\n\n"
    "Выбери действие:"
)

ADMIN_NO_ACCESS = "Нет доступа"

REDUCE_USAGE = "Usage: /reduce <telegram_id> <days>"

REDUCE_PROMPT = (
    "Введи telegram_id и кол-во дней для уменьшения через пробел.\n"
    "Например: <code>123456789 7</code>\n\n"
    "Если новый срок уйдёт в прошлое — юзер будет немедленно кикнут "
    "из всех каналов.\n\n"
    "(Или /cancel чтобы отменить.)"
)

REDUCE_USER_NOT_FOUND = (
    "❌ Пользователь <code>{tg_id}</code> не найден или у него нет подписки."
)

LEGACY_USER_FLAG = (
    "⚠️ <b>Legacy-юзер:</b> не вступал через anti-share flow "
    "(сидит в каналах с до-фикса; кронжоб его не кикнет). "
    "Используй /import_legacy чтобы подцепить."
)

# --- /import_legacy ---
IMPORT_LEGACY_USAGE = "Usage: /import_legacy <telegram_id|@username> <days>"
IMPORT_LEGACY_PROMPT = (
    "Введи <code>&lt;telegram_id или @username&gt; &lt;days&gt;</code>.\n"
    "Например: <code>123456789 30</code>\n\n"
    "Юзер должен быть физически в каналах — это ручное подтверждение от админа.\n\n"
    "(Или /cancel чтобы отменить.)"
)
IMPORT_LEGACY_OK = (
    "✅ Импортирован legacy-юзер <code>{tg_id}</code>.\n"
    "Подписка до: {paid_until_str}\n"
    "Granted_access: {granted} каналов\n"
    "Был создан новый: {created}"
)
IMPORT_LEGACY_RESOLVE_FAIL = "❌ Не удалось резолвить @{username}: {error}"

# --- /bulk_import_legacy ---
BULK_IMPORT_PROMPT = (
    "Вставь список юзеров для legacy-импорта.\n\n"
    "Формат — на каждой строке: <code>&lt;id или @username&gt; &lt;days&gt;</code>\n"
    "Строки начинающиеся с <code>#</code> — игнорируются.\n"
    "Лимит — 100 валидных строк.\n\n"
    "(Или /cancel чтобы отменить.)"
)
BULK_IMPORT_TOO_MANY = "❌ Слишком много валидных строк ({n}). Лимит 100. Разбей на части."
BULK_IMPORT_RESOLVING = "⏳ Резолвлю {n} @username'ов..."
BULK_IMPORT_PREVIEW = (
    "📥 <b>Предпросмотр импорта</b>\n\n"
    "Будет импортировано: {ok}\n"
    "  ├─ создано новых: {new}\n"
    "  └─ продлено существующих: {existing}\n"
    "Ошибок: {errors}{errors_block}\n\n"
    "Подтверди запуск:"
)
BULK_IMPORT_NOTHING_TO_IMPORT = "❌ Нет валидных строк для импорта."
BULK_IMPORT_DONE = (
    "✅ <b>Импорт завершён</b>\n\n"
    "OK: {ok}\n"
    "  ├─ создано новых: {new}\n"
    "  └─ продлено существующих: {existing}\n"
    "Granted_access: {granted}\n"
    "Errors: {errors}"
)

# --- /grant ---
GRANT_USAGE = "Usage: /grant <telegram_id|@username> <days>"
GRANT_PROMPT = (
    "Введи <code>&lt;telegram_id или @username&gt; &lt;days&gt;</code>.\n"
    "Например: <code>123456789 7</code>\n\n"
    "Юзеру будет создана подписка и отправлены invite-ссылки в ЛС.\n"
    "Если юзер ещё не писал боту — ссылки прийдут тебе для пересылки.\n\n"
    "(Или /cancel чтобы отменить.)"
)
GRANT_OK = (
    "🎁 Выдан доступ юзеру <code>{tg_id}</code> на {days} дн.\n"
    "Подписка до: {paid_until_str}\n"
    "Ссылки отправлены в ЛС юзера."
)
GRANT_FORBIDDEN_FALLBACK = (
    "🎁 Выдан доступ юзеру <code>{tg_id}</code> на {days} дн.\n"
    "Подписка до: {paid_until_str}\n\n"
    "⚠️ Юзер ещё не писал боту, ЛС недоступно. Ссылки для пересылки:\n\n{links}"
)
GRANT_NO_LINKS = (
    "⚠️ Доступ юзеру <code>{tg_id}</code> зачислен, но не удалось создать "
    "ни одной invite-ссылки (проблема с правами бота в чатах)."
)

# --- /reissue ---
REISSUE_USAGE = "Usage: /reissue <telegram_id|@username>"
REISSUE_PROMPT = (
    "Введи <code>&lt;telegram_id или @username&gt;</code>, чтобы перевыдать "
    "ссылки.\n\n"
    "Все старые granted_access записи будут revoked, появятся новые invite-ссылки.\n\n"
    "(Или /cancel чтобы отменить.)"
)
REISSUE_OK = (
    "🔁 Перевыдано {n} ссылок юзеру <code>{tg_id}</code>.\n"
    "Старые granted_access revoked. Новые отправлены в ЛС."
)
REISSUE_FORBIDDEN_FALLBACK = (
    "🔁 Перевыдано {n} ссылок юзеру <code>{tg_id}</code>. Старые revoked.\n\n"
    "⚠️ Юзер заблокировал бота, ссылки для пересылки:\n\n{links}"
)
REISSUE_EXPIRED = (
    "❌ Подписка юзера <code>{tg_id}</code> истекла или его нет в БД. "
    "Сначала /grant или /extend."
)
REISSUE_NO_LINKS = (
    "⚠️ Старые granted_access юзера <code>{tg_id}</code> revoked ({revoked}), "
    "но создать новые invite-ссылки не удалось."
)

# --- user-initiated reissue (кнопка в /my) ---
# Префикс MY_ чтобы не путать с админскими REISSUE_* (другая логика, другая
# рассылка). Здесь юзер сам инициирует — мы не пишем «отправлены в ЛС юзера»,
# мы пишем «отправлены тебе».
MY_REISSUE_BUTTON = "🔄 Получить ссылки заново"

MY_REISSUE_OK = (
    "✅ Новые ссылки отправлены. Кликните по любой чтобы войти в каналы.\n"
    "Старые ссылки больше не работают."
)

MY_REISSUE_RATE_LIMITED = (
    "⏱ Запрос слишком частый. Попробуйте через {minutes} мин."
)

MY_REISSUE_EXPIRED = (
    "❌ Подписка истекла. Оформите через главное меню."
)

MY_REISSUE_ERROR = (
    "⚠️ Не удалось выдать ссылки. Напишите в поддержку: {support_url}"
)
