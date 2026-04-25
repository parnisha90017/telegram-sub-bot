"""ChatJoinRequest router: пускаем в каналы только покупателя.

Выдача invite-ссылок ([app/chats/manager.py](issue_invite_links_and_send))
теперь использует `creates_join_request=True`, поэтому каждое вступление
проходит через approve/decline. Здесь сверяем `from_user.id` с тем
`telegram_id`, на который ссылка была выписана (через granted_access),
и заодно проверяем что подписка ещё активна.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Router
from aiogram.types import ChatJoinRequest

from app.db.queries import find_granted_by_link, mark_joined

log = logging.getLogger(__name__)
router = Router()


async def _safe_decline(request: ChatJoinRequest, reason: str) -> None:
    log.info(
        "Decline join: user=%s chat=%s reason=%s",
        request.from_user.id, request.chat.id, reason,
    )
    try:
        await request.decline()
    except Exception as e:
        log.error(
            "decline failed user=%s chat=%s: %s",
            request.from_user.id, request.chat.id, e,
        )


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest) -> None:
    user_tg_id = request.from_user.id
    chat_id = request.chat.id
    invite_link = request.invite_link.invite_link if request.invite_link else None

    if not invite_link:
        # Юзер пришёл без отслеживаемой ссылки — неизвестный канал доступа.
        await _safe_decline(request, "no_invite_link")
        return

    granted = await find_granted_by_link(invite_link)
    if granted is None:
        # Ссылка не наша (или удалена).
        await _safe_decline(request, "unknown_invite_link")
        return

    if user_tg_id != granted["telegram_id"]:
        # Кто-то другой пытается войти по чужой ссылке (перепродажа доступа).
        await _safe_decline(
            request,
            f"user_mismatch_expected_{granted['telegram_id']}",
        )
        return

    paid_until: datetime = granted["paid_until"]
    if paid_until < datetime.now(timezone.utc):
        await _safe_decline(request, f"subscription_expired_{paid_until.isoformat()}")
        return

    try:
        await request.approve()
    except Exception as e:
        log.error("approve failed user=%s chat=%s: %s", user_tg_id, chat_id, e)
        return

    try:
        await mark_joined(invite_link)
    except Exception as e:
        # Approve уже прошёл — это не блокер, просто логируем.
        log.error("mark_joined failed for link=%s: %s", invite_link, e)

    log.info("Approved join: user=%s chat=%s", user_tg_id, chat_id)
