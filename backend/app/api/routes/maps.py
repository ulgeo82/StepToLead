"""Ads → Yandex Maps / 2GIS channels: spend per month, statistics files, call tracking numbers and the UTM link."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.ads import require_manage_integrations, scoped_connection
from app.api.routes.result import reporting_period, scoped_project
from app.core.access import check_origin
from app.core.crypto import encrypt_secret
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.marketing import AdConnection, ClientWorkspace
from app.models.telephony import TelephonyConnection
from app.services import maps

router = APIRouter(prefix="/ads/maps", tags=["ads-maps"])
UTM = r"^[a-z0-9_.-]{2,40}$"


class MapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    platform: str = Field(pattern=r"^(yandex_maps|2gis)$")
    name: str = Field(min_length=2, max_length=180)
    card_url: str = Field(default="", max_length=500, pattern=r"^(https://\S+)?$")
    utm_source: str = Field(default="", max_length=40)


class MapPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    card_url: str | None = Field(default=None, max_length=500, pattern=r"^(https://\S+)?$")
    utm_source: str | None = Field(default=None, max_length=40, pattern=UTM)


class MonthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    budget: float | None = Field(default=None, ge=0, le=100_000_000)
    impressions: int | None = Field(default=None, ge=0, le=100_000_000)
    clicks: int | None = Field(default=None, ge=0, le=100_000_000)
    calls: int | None = Field(default=None, ge=0, le=1_000_000)
    routes: int | None = Field(default=None, ge=0, le=1_000_000)
    site: int | None = Field(default=None, ge=0, le=1_000_000)


async def readable(db: AsyncSession, request: Request, connection_id: int):
    row = await db.get(AdConnection, connection_id)
    if not maps.is_map(row) or row.project_id is None:
        raise HTTPException(404, "Канал не найден")
    project, kind, user = await scoped_project(db, request, row.project_id)
    if row.workspace_id != project.workspace_id:
        raise HTTPException(404, "Канал не найден")
    require_actor_permission(kind, user, "view_ads")
    return row, project


async def editable(db: AsyncSession, request: Request, connection_id: int) -> AdConnection:
    check_origin(request)
    row = await scoped_connection(db, request, connection_id)
    if not maps.is_map(row):
        raise HTTPException(404, "Канал не найден")
    return row


async def tracking_numbers(db: AsyncSession, row: AdConnection) -> list[str]:
    conns = (await db.scalars(select(TelephonyConnection).where(TelephonyConnection.project_id == row.project_id))).all()
    return sorted(n for c in conns for n, route in ((c.config or {}).get("line_map") or {}).items()
                  if route.get("kind") == "ad" and int(route.get("id") or 0) == row.id)


@router.post("", status_code=201)
async def create_map(payload: MapIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    await require_manage_integrations(db, project, kind, user)
    source = (payload.utm_source or maps.DEFAULT_UTM[payload.platform]).strip().lower()
    taken = await maps.connection_for_utm(db, project.id, source)
    if taken:
        raise HTTPException(409, f"Метка utm_source={source} уже у другого канала карт — укажите другую, например {source}_2")
    row = AdConnection(workspace_id=project.workspace_id, project_id=project.id, platform=payload.platform, name=payload.name.strip(),
                       external_account_id=payload.card_url or "card", access_token_encrypted=encrypt_secret("no-api"),
                       status="connected", currency="RUB", config={"card_url": payload.card_url, "utm_source": source})
    db.add(row)
    await db.commit()
    return {"id": row.id, "platform": row.platform, "name": row.name}


@router.get("/{connection_id}")
async def map_detail(connection_id: int, request: Request, start: date | None = None, end: date | None = None,
                     db: AsyncSession = Depends(get_db)):
    row, project = await readable(db, request, connection_id)
    start, end = reporting_period(start, end)
    config = row.config or {}
    workspace = await db.get(ClientWorkspace, row.workspace_id)
    site = ((workspace.website if workspace else None) or "https://ваш-сайт.ru").rstrip("/")
    source = maps.utm_source(row)
    return {"id": row.id, "platform": row.platform, "platform_name": maps.PLATFORMS[row.platform], "name": row.name,
            "card_url": config.get("card_url") or "", "utm_source": source,
            "utm_link": f"{site}/?utm_source={source}&utm_medium=maps&utm_campaign=card",
            "budgets": [{"month": m, "amount": a} for m, a in sorted((config.get("budgets") or {}).items(), reverse=True)],
            "manual": config.get("manual") or {}, "tracking_numbers": await tracking_numbers(db, row),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "totals": await maps.totals(db, row, start, end), "last_upload": config.get("last_upload")}


@router.patch("/{connection_id}")
async def update_map(connection_id: int, payload: MapPatch, request: Request, db: AsyncSession = Depends(get_db)):
    row = await editable(db, request, connection_id)
    config = dict(row.config or {})
    if payload.utm_source and payload.utm_source != maps.utm_source(row):
        if await maps.connection_for_utm(db, row.project_id, payload.utm_source):
            raise HTTPException(409, "Эта метка уже у другого канала карт")
        config["utm_source"] = payload.utm_source
    if payload.card_url is not None:
        config["card_url"] = payload.card_url
    if payload.name:
        row.name = payload.name.strip()
    row.config = config
    await db.commit()
    return {"ok": True}


@router.put("/{connection_id}/month")
async def set_month(connection_id: int, payload: MonthIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Monthly promotion cost (spread over the month's days) and, for 2GIS, monthly card statistics typed in."""
    row = await editable(db, request, connection_id)
    totals = {k: getattr(payload, k) for k in ("impressions", "clicks", *maps.EXTRA) if getattr(payload, k) is not None}
    if payload.budget is None and not totals:
        raise HTTPException(422, "Укажите расход или цифры статистики за месяц")
    await maps.set_month(db, row, payload.month, budget=payload.budget, totals=totals or None)
    await db.commit()
    return {"ok": True}


@router.post("/{connection_id}/stats")
async def upload_stats(connection_id: int, request: Request, file: UploadFile, db: AsyncSession = Depends(get_db)):
    row = await editable(db, request, connection_id)
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 10 МБ")
    try:
        days, found = maps.parse_stats(file.filename or "", content)
    except maps.StatsError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(422, "Не удалось прочитать файл. Сохраните выгрузку в XLSX и попробуйте снова") from None
    result = await maps.import_days(db, row, days)
    row.config = {**(row.config or {}), "last_upload": {**result, "metrics": found, "file": (file.filename or "")[:120]}}
    from datetime import datetime, timezone
    row.last_synced_at = datetime.now(timezone.utc)
    await db.commit()
    return {**result, "metrics": found}
