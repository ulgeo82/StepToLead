"""/api/telegram-warmup — прогрев Telegram-аккаунтов выбранного клиента (docs/TELEGRAM_WARMUP.md)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.domains.leadgen import space
from app.models import TelegramAccount
from app.models.telegram_warmup import TelegramWarmupEvent
from app.services import tg_limits, tg_space, tg_warmup

router = APIRouter(prefix="/telegram-warmup", tags=["telegram-warmup"], dependencies=[Depends(space.bind)])


class SettingsIn(BaseModel):
    intensity: Literal["soft", "normal"] | None = None
    channels: list[str] | None = Field(default=None, max_length=30)
    pool: bool | None = None


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    return await tg_warmup.company(db, tg_space.current())


@router.put("/settings")
async def put_settings(payload: SettingsIn, db: AsyncSession = Depends(get_db)):
    out = await tg_warmup.save_company(db, tg_space.current(), payload.model_dump(exclude_none=True))
    await db.commit()
    return out


async def _accounts(db: AsyncSession, ids: list[int] | None = None) -> list[TelegramAccount]:
    query = tg_space.scope(select(TelegramAccount), TelegramAccount.workspace_id).order_by(TelegramAccount.id)
    if ids is not None:
        query = query.where(TelegramAccount.id.in_(ids))
    return list((await db.scalars(query)).all())


@router.get("/accounts")
async def accounts(db: AsyncSession = Depends(get_db)):
    now = tg_warmup.now_utc()
    limit = (await tg_limits.company(db, tg_space.current()))["first_per_day"]
    rows = await _accounts(db)
    return [{"id": a.id, "phone": a.phone, "username": a.username, "name": " ".join(filter(None, [a.first_name, a.last_name])),
             "avatar_thumb": a.avatar_thumb, "status": a.status, "proxy_id": a.proxy_id,
             "daily_first_limit": a.daily_first_limit, **tg_warmup.view(a, now, limit)} for a in rows]


@router.get("/events")
async def events(account_id: int | None = None, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    ids = [a.id for a in await _accounts(db)]
    query = select(TelegramWarmupEvent).where(TelegramWarmupEvent.account_id.in_(ids or [0]))
    if account_id:
        query = query.where(TelegramWarmupEvent.account_id == account_id)
    rows = (await db.scalars(query.order_by(TelegramWarmupEvent.id.desc()).limit(limit))).all()
    return [{"id": e.id, "account_id": e.account_id, "at": e.at.isoformat() if e.at else None, "kind": e.kind,
             "text": e.text} for e in rows]


@router.post("/{action}")
async def act(action: Literal["start", "pause", "resume", "warmed", "reset"], payload: IdsIn,
              db: AsyncSession = Depends(get_db)):
    rows = await _accounts(db, payload.ids)
    if not rows:
        raise HTTPException(404, "Аккаунты не найдены")
    if action == "start":
        done = await tg_warmup.start(db, rows)
    elif action == "warmed":
        done = await tg_warmup.start(db, rows, warmed=True)
    elif action == "reset":
        done = await tg_warmup.reset(db, rows)
    else:
        done = await tg_warmup.pause(db, rows, paused=action == "pause")
    await db.commit()
    return {"done": done}


@router.get("/accounts/{account_id}/twofa")
async def twofa(account_id: int, db: AsyncSession = Depends(get_db)):
    """Облачный пароль, который поставил прогрев — для входа в аккаунт с телефона. Только агентству."""
    from app.core.crypto import decrypt_secret
    account = tg_space.require(await db.get(TelegramAccount, account_id), "Аккаунт не найден")
    if not account.twofa_encrypted:
        raise HTTPException(404, "Прогрев не ставил облачный пароль этому аккаунту")
    return {"password": decrypt_secret(account.twofa_encrypted)}
