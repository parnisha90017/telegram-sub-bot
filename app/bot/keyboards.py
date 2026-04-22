from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SUPPORT_URL = "https://t.me/alfasignal_t"

PLANS: dict[str, dict[str, int | str]] = {
    "tariff_3d":  {"title": "3 дня — 11 USDT",   "amount": 11, "days": 3},
    "tariff_7d":  {"title": "7 дней — 21 USDT",  "amount": 21, "days": 7},
    "tariff_30d": {"title": "30 дней — 60 USDT", "amount": 60, "days": 30},
}


def main_menu_kb(is_active: bool = False) -> InlineKeyboardMarkup:
    pay_label = "🔄 Продлить" if is_active else "💳 Оплатить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pay_label, callback_data="show_plans")],
        [InlineKeyboardButton(text="🛠 Тех. поддержка", url=SUPPORT_URL)],
        [InlineKeyboardButton(text="ℹ️ О нас", callback_data="about")],
    ])


def plans_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=str(plan["title"]), callback_data=f"buy:{key}")]
        for key, plan in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
