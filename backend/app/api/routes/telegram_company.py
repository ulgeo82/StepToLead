"""/api/telegram-company — Telegram-рассылка компании и её гипотезы (docs/TELEGRAM_COMPANY.md)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.domains.leadgen import space
from app.models import Campaign
from app.services import tg_company, tg_space

router = APIRouter(prefix="/telegram-company", tags=["telegram-company"], dependencies=[Depends(space.bind)])


def ws() -> int:
    current = tg_space.current()
    if current is None:
        raise HTTPException(503, "Не задан AGENCY_WORKSPACE_ID")
    return current


class SettingsIn(BaseModel):
    settings: dict
    api_key: str | None = Field(default=None, max_length=1000)


class HypothesisIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


@router.get("")
async def overview(db: AsyncSession = Depends(get_db)):
    data = await tg_company.settings(db, ws())
    data["hypotheses"] = await tg_company.overview(db, ws())
    data["accounts"] = len(await tg_company.working_accounts(db, ws()))
    await db.commit()
    return data


@router.put("/settings")
async def save(payload: SettingsIn, db: AsyncSession = Depends(get_db)):
    try:
        out = await tg_company.save_settings(db, ws(), payload.settings, payload.api_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.commit()
    return out


@router.post("/hypotheses", status_code=201)
async def create(payload: HypothesisIn, db: AsyncSession = Depends(get_db)):
    c = Campaign(name=payload.name.strip(), description=payload.description, workspace_id=ws())
    db.add(c)
    await db.flush()
    await tg_company.apply(db, ws())
    await db.commit()
    return {"id": c.id, "name": c.name}


@router.post("/hypotheses/{campaign_id}/activate")
async def activate(campaign_id: int, db: AsyncSession = Depends(get_db)):
    await tg_company.activate(db, ws(), campaign_id)
    await db.commit()
    st = await tg_company.state(db, ws())
    if st.get("status") == "running":   # рассылка идёт — сразу запускаем новую активную гипотезу
        await _start(campaign_id, db)
    return await overview(db)


@router.post("/deactivate")
async def deactivate(db: AsyncSession = Depends(get_db)):
    await tg_company.deactivate(db, ws())
    await db.commit()
    return await overview(db)


async def _start(campaign_id: int, db: AsyncSession):
    from app.api.routes.automation import start_campaign
    await start_campaign(campaign_id, db)


@router.post("/start")
async def start(db: AsyncSession = Depends(get_db)):
    await tg_company.set_status(db, ws(), True, lambda cid: _start(cid, db))
    return await overview(db)


@router.post("/pause")
async def pause(db: AsyncSession = Depends(get_db)):
    await tg_company.set_status(db, ws(), False, None)
    return await overview(db)
