"""Link a portal user's Telegram to the notification bot.

Flow without a public webhook: the cabinet creates a one-time code and opens
t.me/<bot>?start=<code>; the user presses Start; the cabinet then calls /check,
which reads pending bot updates (getUpdates) and binds the chat to the user.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_portal_user, token_digest
from app.db import get_db
from app.models.marketing import PortalUser
from app.services.notifications import telegram_api, telegram_configured

router = APIRouter(prefix="/portal/telegram", tags=["portal-telegram"])
logger = logging.getLogger("uvicorn.error.telegram_bot")
LINK_TTL = timedelta(minutes=15)
_bot_username: str | None = None


async def bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await telegram_api("getMe"))["username"]
    return _bot_username


def _status(user: PortalUser, username: str | None = None) -> dict:
    return {"configured": telegram_configured(), "linked": bool(user.telegram_chat_id),
            "telegram_username": user.telegram_username, "bot_username": username}


def _require_bot():
    if not telegram_configured():
        raise HTTPException(409, "Telegram-бот не настроен: задайте TELEGRAM_BOT_TOKEN на сервере")


@router.get("")
async def telegram_status(user: PortalUser = Depends(require_portal_user)):
    username = None
    if telegram_configured():
        try:
            username = await bot_username()
        except Exception as exc:
            logger.warning("telegram getMe failed: %s", exc)
    return _status(user, username)


@router.post("/link")
async def start_link(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    _require_bot()
    try:
        username = await bot_username()
    except Exception as exc:
        raise HTTPException(502, f"Telegram недоступен: {exc}") from None
    code = secrets.token_urlsafe(24)
    user.telegram_link_code_hash = token_digest(code)
    user.telegram_link_expires_at = datetime.now(timezone.utc) + LINK_TTL
    await db.commit()
    return {"url": f"https://t.me/{username}?start={code}", "expires_in": int(LINK_TTL.total_seconds())}


async def _consume_updates(db: AsyncSession) -> None:
    updates = await telegram_api("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    if not updates:
        return
    now = datetime.now(timezone.utc)
    for update in updates:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        if not text.startswith("/start") or chat.get("type") != "private":
            continue
        code = text.split(maxsplit=1)[1] if " " in text else ""
        target = await db.scalar(select(PortalUser).where(
            PortalUser.telegram_link_code_hash == token_digest(code))) if code else None
        expires = target.telegram_link_expires_at if target else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if target is None or expires is None or expires < now:
            reply = "Ссылка устарела или неверна. Нажмите «Подключить Telegram» в кабинете StepToLead ещё раз."
        else:
            target.telegram_chat_id = str(chat["id"])
            target.telegram_username = (message.get("from") or {}).get("username")
            target.telegram_link_code_hash = None
            target.telegram_link_expires_at = None
            reply = f"Готово! {target.display_name}, сюда будут приходить уведомления StepToLead."
        try:
            await telegram_api("sendMessage", {"chat_id": chat["id"], "text": reply})
        except Exception as exc:
            logger.warning("telegram reply failed: %s", exc)
    await db.commit()
    # Acknowledge processed updates so they are not read again.
    await telegram_api("getUpdates", {"offset": max(u["update_id"] for u in updates) + 1, "timeout": 0})


@router.post("/check")
async def check_link(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    _require_bot()
    try:
        await _consume_updates(db)
    except Exception as exc:
        raise HTTPException(502, f"Не удалось получить ответ от Telegram: {exc}") from None
    await db.refresh(user)
    return _status(user)


@router.post("/test")
async def send_test(user: PortalUser = Depends(require_portal_user)):
    _require_bot()
    if not user.telegram_chat_id:
        raise HTTPException(409, "Telegram ещё не подключён")
    try:
        await telegram_api("sendMessage", {"chat_id": user.telegram_chat_id,
                                           "text": "Тестовое уведомление StepToLead: всё работает ✅"})
    except Exception as exc:
        raise HTTPException(502, f"Telegram не принял сообщение: {exc}") from None
    return {"ok": True}


@router.delete("")
async def unlink(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    user.telegram_chat_id = None
    user.telegram_username = None
    user.telegram_link_code_hash = None
    user.telegram_link_expires_at = None
    await db.commit()
    return _status(user)
