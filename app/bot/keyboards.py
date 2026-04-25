from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.texts import MY_REISSUE_BUTTON

SUPPORT_URL = "https://t.me/alfasignal_t"

PLANS: dict[str, dict[str, int | str]] = {
    "tariff_3d":  {"title": "3 дня — 11 USDT",   "amount": 11, "days": 3},
    "tariff_7d":  {"title": "7 дней — 21 USDT",  "amount": 21, "days": 7},
    "tariff_30d": {"title": "30 дней — 60 USDT", "amount": 60, "days": 30},
}

PROVIDER_LABELS: dict[str, str] = {
    "cryptobot": "💎 CryptoBot",
    "heleket":   "🔷 TRC-20 USDT",
}

# Строгий порядок отображения кнопок провайдеров (вне зависимости от
# порядка в settings.enabled_providers).
PROVIDER_ORDER: tuple[str, ...] = ("cryptobot", "heleket")


def main_menu_kb(is_active: bool = False) -> InlineKeyboardMarkup:
    pay_label = "🔄 Продлить" if is_active else "💳 Оплатить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pay_label, callback_data="show_plans")],
        [InlineKeyboardButton(text="📋 Моя подписка", callback_data="show_my")],
        [InlineKeyboardButton(text="🛠 Тех. поддержка", url=SUPPORT_URL)],
        [InlineKeyboardButton(text="ℹ️ О нас", callback_data="about")],
    ])


def my_subscription_kb(active: bool) -> InlineKeyboardMarkup:
    """Клавиатура под ответом /my. Кнопка «Получить ссылки заново» появляется
    только для активных подписчиков — для expired показывать бессмысленно
    (callback всё равно ответит MY_REISSUE_EXPIRED)."""
    rows: list[list[InlineKeyboardButton]] = []
    if active:
        rows.append([InlineKeyboardButton(
            text=MY_REISSUE_BUTTON,
            callback_data="user:reissue_links",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=str(plan["title"]), callback_data=f"buy:{key}")]
        for key, plan in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    """Inline-панель админа. Каждая кнопка — на свою строку (длинные
    подписи с описанием)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика — оборот, активные", callback_data="admin:stats")
    builder.button(text="🔍 Найти юзера — по ID или @username", callback_data="admin:find")
    builder.button(text="📋 Excel-выгрузка — все юзеры", callback_data="admin:export")
    builder.button(text="➕ Добавить дни — без оплаты", callback_data="admin:extend")
    builder.button(text="➖ Убрать дни — кикнет если уйдёт в expired", callback_data="admin:reduce")
    builder.button(text="🎁 Выдать доступ — новый юзер + ссылки в ЛС", callback_data="admin:grant")
    builder.button(text="🔁 Перевыдать ссылки — revoke старые + новые", callback_data="admin:reissue")
    builder.button(text="📥 Импорт legacy — подцепить юзера из чатов", callback_data="admin:import_legacy")
    builder.button(text="📥📥 Bulk-импорт legacy — список", callback_data="admin:bulk_import")
    builder.button(text="🚫 Отозвать доступ — кикнуть из чатов", callback_data="admin:revoke")
    builder.button(text="🧹 Анализ чатов — БД vs участники", callback_data="admin:cleanup")
    builder.button(text="⏳ Зависшие платежи — pending >1ч", callback_data="admin:pending")
    builder.button(text="🏥 Здоровье — DB, провайдеры, чаты", callback_data="admin:health")
    builder.button(text="❌ Закрыть", callback_data="admin:close")
    builder.adjust(1)
    return builder.as_markup()


def bulk_import_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Запустить", callback_data="admin:bulk_import_confirm")
    builder.button(text="❌ Отмена", callback_data="admin:close")
    builder.adjust(1)
    return builder.as_markup()


def revoke_confirm_kb(tg_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, отозвать", callback_data=f"admin:revoke_confirm:{tg_id}")
    builder.button(text="❌ Отмена", callback_data="admin:close")
    builder.adjust(1)
    return builder.as_markup()


def provider_pick_kb(plan_key: str, enabled_providers: list[str]) -> InlineKeyboardMarkup:
    """Кнопки активных провайдеров + Назад к списку тарифов."""
    builder = InlineKeyboardBuilder()
    for code in PROVIDER_ORDER:
        if code in enabled_providers and code in PROVIDER_LABELS:
            builder.button(
                text=PROVIDER_LABELS[code],
                callback_data=f"pay:{plan_key}:{code}",
            )
    builder.button(text="◀️ Назад", callback_data="show_plans")
    builder.adjust(1)
    return builder.as_markup()
