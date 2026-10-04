"""Admin → «Мониторинг»: current failures of the portal and the Telegram chat that receives alerts."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_admin, token_digest
from app.db import get_db
from app.models.access import AdminUser
from app.services import monitor
from app.services.notifications import telegram_api, telegram_configured

router = APIRouter(prefix="/admin/monitor", tags=["admin-monitor"])


def telegram_state(admin: AdminUser) -> dict:
    return {"configured": telegram_configured(), "linked": bool(admin.telegram_chat_id), "username": admin.telegram_username}


@router.get("")
async def overview(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    found = await monitor.problems(db)
    return {"problems": found, "telegram": telegram_state(admin),
            "workers": [{"name": n, "ok": not t.done()} for n, t in monitor._tasks.items()]}


@router.post("/telegram/link")
async def link(request: Request, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    check_origin(request)
    if not telegram_configured():
        raise HTTPException(409, "Telegram-бот не настроен: задайте TELEGRAM_BOT_TOKEN на сервере")
    from app.api.routes.telegram_bot import bot_username
    try:
        username = await bot_username()
    except Exception as exc:
        raise HTTPException(502, f"Telegram недоступен: {exc}") from None
    code = secrets.token_urlsafe(24)
    admin.telegram_link_code_hash = token_digest(code)
    admin.telegram_link_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    await db.commit()
    return {"url": f"https://t.me/{username}?start={code}"}


@router.post("/telegram/check")
async def check(request: Request, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    check_origin(request)
    await db.refresh(admin)
    return telegram_state(admin)


@router.post("/telegram/test")
async def test(request: Request, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    check_origin(request)
    if not admin.telegram_chat_id:
        raise HTTPException(409, "Telegram ещё не подключён")
    try:
        await telegram_api("sendMessage", {"chat_id": admin.telegram_chat_id, "text": "Тест: уведомления о сбоях StepToLead работают ✅"})
    except Exception as exc:
        raise HTTPException(502, f"Telegram не принял сообщение: {exc}") from None
    return {"ok": True}


@router.delete("/telegram")
async def unlink(request: Request, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    check_origin(request)
    admin.telegram_chat_id = admin.telegram_username = admin.telegram_link_code_hash = None
    admin.telegram_link_expires_at = None
    await db.commit()
    return telegram_state(admin)
