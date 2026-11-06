"""Нажатия 👍/👎 под постами радара. Приходят через общий обработчик бота портала (tg_bot.process_update),
второго опросчика бота нет. callback_data: jr:<id совпадения>:<1|-1>."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar import posting
from app.domains.jobradar.models import JrMatch
from app.models.access import AdminUser

PREFIX = "jr:"


def is_jobradar(query: dict | None) -> bool:
    return bool(query) and str(query.get("data") or "").startswith(PREFIX)


async def apply(db: AsyncSession, query: dict) -> dict | None:
    """Сохраняет оценку. Возвращает payload для editMessageReplyMarkup (или None — ничего не меняем).
    Оценивать может только админ агентства, привязавший Telegram в портале: канал бывает публичным."""
    parts = str(query.get("data") or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("1", "-1"):
        return None
    sender = str((query.get("from") or {}).get("id") or "")
    if not sender or not await db.scalar(select(AdminUser.id).where(AdminUser.telegram_chat_id == sender).limit(1)):
        return None
    message = query.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    match = await db.get(JrMatch, int(parts[1]))
    if match is None or match.chat_id != chat_id or match.message_id != message.get("message_id"):
        return None
    value = int(parts[2])
    match.feedback, match.feedback_at = value, datetime.now(timezone.utc)
    await db.flush()
    return {"chat_id": chat_id, "message_id": match.message_id, "reply_markup": posting.done_keyboard(value)}
