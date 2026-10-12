"""Link a portal user's Telegram to the notification bot.

The worker services/tg_bot.py is the sole update consumer. /check only reads DB.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
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


async def _company_bot(db: AsyncSession, user: PortalUser):
    from app.services import client_bots
    row = await client_bots.bot_for(db, user.workspace_id)
    return row if row and row.active and row.username else None


async def _status(db: AsyncSession, user: PortalUser, username: str | None = None) -> dict:
    company_bot = await _company_bot(db, user)
    linked_to_company = bool(company_bot and (user.telegram_state or {}).get("bot_id") == company_bot.id)
    return {"configured": telegram_configured() or bool(company_bot), "linked": bool(user.telegram_chat_id),
            "telegram_username": user.telegram_username,
            "bot_username": company_bot.username if company_bot else username,
            "company_bot": bool(company_bot),
            # Подключён к боту агентства, а у компании уже свой бот — предлагаем переподключиться.
            "relink_suggested": bool(company_bot and user.telegram_chat_id and not linked_to_company)}


async def _require_bot(db: AsyncSession, user: PortalUser):
    if not telegram_configured() and not await _company_bot(db, user):
        raise HTTPException(409, "Telegram-бот не настроен: подключите бот компании в портале")


@router.get("")
async def telegram_status(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    username = None
    if telegram_configured() and not await _company_bot(db, user):
        try:
            username = await bot_username()
        except Exception as exc:
            logger.warning("telegram getMe failed: %s", exc)
    return await _status(db, user, username)


@router.post("/link")
async def start_link(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    await _require_bot(db, user)
    company_bot = await _company_bot(db, user)
    try:
        username = company_bot.username if company_bot else await bot_username()
    except Exception as exc:
        raise HTTPException(502, f"Telegram недоступен: {exc}") from None
    code = secrets.token_urlsafe(24)
    user.telegram_link_code_hash = token_digest(code)
    user.telegram_link_expires_at = datetime.now(timezone.utc) + LINK_TTL
    await db.commit()
    return {"url": f"https://t.me/{username}?start={code}", "expires_in": int(LINK_TTL.total_seconds())}


@router.post("/check")
async def check_link(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    await _require_bot(db, user)
    await db.refresh(user)
    return await _status(db, user)


@router.post("/test")
async def send_test(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    await _require_bot(db, user)
    if not user.telegram_chat_id:
        raise HTTPException(409, "Telegram ещё не подключён")
    from app.services.client_bots import send_to_user
    try:
        await send_to_user(db, user, "sendMessage", {"chat_id": user.telegram_chat_id,
                                                     "text": "Тестовое уведомление: всё работает ✅"})
    except Exception as exc:
        raise HTTPException(502, f"Telegram не принял сообщение: {exc}") from None
    return {"ok": True}


@router.delete("")
async def unlink(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    user.telegram_chat_id = None
    user.telegram_username = None
    user.telegram_link_code_hash = None
    user.telegram_link_expires_at = None
    user.telegram_state = {k: v for k, v in (user.telegram_state or {}).items() if k != "bot_id"}
    await db.commit()
    return await _status(db, user)
