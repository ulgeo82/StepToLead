"""Project-scoped advertising overview built from the shared result facts.

Only AdConnection rows are advertising accounts. Generic ProjectSource rows
remain in /result and /analytics and are deliberately excluded here.
"""
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.routes import marketing
from app.api.routes.result import reporting_period, scoped_project
from app.core.access import check_origin
from app.core.permissions import effective_permissions, require_actor_permission
from app.db import get_db
from app.models.marketing import AdConnection, AdMetricDaily, PortalProjectAccess
from app.services.result_analytics import _bucket, _sum, result_facts

router = APIRouter(prefix="/ads", tags=["ads"])


class ConnectAdAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    platform: str
    name: str = Field(min_length=2, max_length=180)
    external_account_id: str = Field(default="", max_length=180)
    access_token: str | None = Field(default=None, min_length=10, max_length=4096)
    client_id: str | None = Field(default=None, min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, min_length=10, max_length=4096)


async def require_manage_integrations(db: AsyncSession, project, kind, user):
    if kind == "admin":
        return
    if user.role == "client_owner":
        return
    access = await db.scalar(select(PortalProjectAccess).where(
        PortalProjectAccess.user_id == user.id, PortalProjectAccess.project_id == project.id))
    allowed = "manage_integrations" in effective_permissions(user)
    if user.permissions is None and access and access.manage_integrations:
        allowed = True  # preserve existing project grants
    if not allowed or not access:
        raise HTTPException(403, "Для этого проекта не выдано право manage_integrations")


async def scoped_connection(db: AsyncSession, request: Request, connection_id: int):
    row = await db.get(AdConnection, connection_id)
    if not row or row.project_id is None or row.platform == "telegram_ads":
        raise HTTPException(404, "Рекламный кабинет не найден")
    project, kind, user = await scoped_project(db, request, row.project_id)
    if row.workspace_id != project.workspace_id:
        raise HTTPException(404, "Рекламный кабинет не найден")
    await require_manage_integrations(db, project, kind, user)
    return row


async def readable_vk_connection(db: AsyncSession, request: Request, connection_id: int):
    row = await db.get(AdConnection, connection_id)
    if not row or row.project_id is None or row.platform != "vk_ads":
        raise HTTPException(404, "Кабинет VK не найден")
    project, kind, user = await scoped_project(db, request, row.project_id)
    if row.workspace_id != project.workspace_id:
        raise HTTPException(404, "Кабинет VK не найден")
    require_actor_permission(kind, user, "view_ads")
    if row.status != "connected":
        raise HTTPException(409, "Сначала проверьте подключение кабинета VK")
    return row, project


async def verified_vk_token(db: AsyncSession, row: AdConnection) -> str:
    token = await marketing._vk_access_token(db, row)
    user = await run_in_threadpool(marketing._vk_json, "/api/v3/user.json", token)
    if str(user.get("id")) != row.external_account_id:
        raise HTTPException(409, "Данные доступа VK не соответствуют подключенному кабинету")
    return token


def _ratio(top, bottom):
    return top / bottom if top is not None and bottom not in (None, 0) else None


@router.get("")
async def ads_overview(request: Request, project_id: int, start: date | None = None, end: date | None = None,
                       granularity: str = "day", db: AsyncSession = Depends(get_db)):
    if granularity not in {"day", "week", "month"}:
        raise HTTPException(422, "Доступна группировка по дням, неделям или месяцам")
    start, end = reporting_period(start, end)
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_ads")
    facts = await result_facts(db, project, start, end, granularity, include_source_trend=True)
    connections = (await db.scalars(select(AdConnection).where(AdConnection.project_id == project.id,
                                                      AdConnection.platform != "telegram_ads")
                                    .order_by(AdConnection.platform, AdConnection.name, AdConnection.id))).all()
    ids = [row.id for row in connections]
    previous_start = facts["period"]["previous_start"]
    previous_end = facts["period"]["previous_end"]
    ad_rows = (await db.scalars(select(AdMetricDaily).where(
        AdMetricDaily.connection_id.in_(ids), AdMetricDaily.date >= previous_start,
        AdMetricDaily.date <= end))).all() if ids else []

    def metrics(account_ids: list[int], period_start: date, period_end: date, period: dict):
        source_map = {row["id"]: row for row in period["sources"]}
        source_values = [source_map[f"ad:{item}"] for item in account_ids if f"ad:{item}" in source_map]
        spend = _sum(item["spend"] for item in source_values)
        leads = _sum(item["leads"] for item in source_values)
        sales = _sum(item["sales"] for item in source_values)
        revenue = _sum(item["revenue"] for item in source_values)
        clicks_rows = [row for row in ad_rows if row.connection_id in account_ids and period_start <= row.date <= period_end]
        clicks = sum(int(row.clicks or 0) for row in clicks_rows) if clicks_rows else None
        margin = facts["economics"]["margin"]
        gross_profit = revenue * margin / 100 if revenue is not None and margin is not None else None
        romi = ((gross_profit - spend) / spend * 100 if gross_profit is not None and spend not in (None, 0)
                and sales not in (None, 0) and revenue not in (None, 0) else None)
        return {"spend": spend, "clicks": clicks, "cpc": _ratio(spend, clicks),
                "leads": int(leads) if leads is not None else None, "cpl": _ratio(spend, leads),
                "sales": int(sales) if sales is not None else None, "revenue": revenue,
                "gross_profit": gross_profit, "romi": romi}

    def trend(account_ids: list[int], period_start: date, period_end: date, period: dict):
        labels = [point["label"] for point in period["trend"]]
        by_label = {label: {"label": label, "spend": None, "clicks": None, "leads": None,
                            "cpc": None, "cpl": None} for label in labels}
        for connection_id in account_ids:
            for point in period["source_trend"].get(f"ad:{connection_id}", []):
                target = by_label[point["label"]]
                for field in ("spend", "leads"):
                    if point[field] is not None:
                        target[field] = (target[field] or 0) + point[field]
        for row in ad_rows:
            if row.connection_id in account_ids and period_start <= row.date <= period_end:
                cell = by_label[_bucket(row.date, granularity)]
                cell["clicks"] = (cell["clicks"] or 0) + int(row.clicks or 0)
        for cell in by_label.values():
            cell["cpc"] = _ratio(cell["spend"], cell["clicks"])
            cell["cpl"] = _ratio(cell["spend"], cell["leads"])
        return list(by_label.values())

    def snapshot(account_ids: list[int], period_start: date, period_end: date, period: dict):
        return {**metrics(account_ids, period_start, period_end, period),
                "trend": trend(account_ids, period_start, period_end, period)}

    groups = defaultdict(list)
    for row in connections:
        groups[row.platform].append(row)
    platforms = []
    for platform, accounts in sorted(groups.items()):
        account_rows = []
        for account in accounts:
            account_rows.append({"id": account.id, "name": account.name,
                                 "external_account_id": account.external_account_id,
                                 "status": account.status, "last_error": account.last_error,
                                 "last_synced_at": account.last_synced_at,
                                 "current": snapshot([account.id], start, end, facts["current"]),
                                 "previous": snapshot([account.id], previous_start, previous_end, facts["previous"])})
        account_ids = [row.id for row in accounts]
        statuses = {row.status for row in accounts}
        status = ("syncing" if "syncing" in statuses else "error" if "error" in statuses else
                  "connected" if "connected" in statuses else "disconnected" if statuses == {"disconnected"} else "pending")
        sync_times = [row.last_synced_at for row in accounts if row.last_synced_at]
        platforms.append({"id": platform, "name": marketing.SUPPORTED.get(platform, platform),
                          "account_count": len(accounts), "connected_count": sum(row.status == "connected" for row in accounts),
                          "status": status, "last_synced_at": max(sync_times) if sync_times else None,
                          "current": snapshot(account_ids, start, end, facts["current"]),
                          "previous": snapshot(account_ids, previous_start, previous_end, facts["previous"]),
                          "accounts": account_rows})
    all_ids = [row.id for row in connections]
    can_manage = kind == "admin" or ("manage_integrations" in effective_permissions(user))
    if kind == "client" and user.role != "client_owner":
        access = await db.scalar(select(PortalProjectAccess).where(
            PortalProjectAccess.user_id == user.id, PortalProjectAccess.project_id == project.id))
        can_manage = bool(access and (can_manage or (user.permissions is None and access.manage_integrations)))
    return {"project": {"id": project.id, "name": project.name}, "period": facts["period"],
            "viewer": {"role": "admin" if kind == "admin" else user.role,
                       "can_manage_integrations": can_manage},
            "supported_platforms": [{"id": key, "name": value} for key, value in marketing.SUPPORTED.items()],
            "current": snapshot(all_ids, start, end, facts["current"]),
            "previous": snapshot(all_ids, previous_start, previous_end, facts["previous"]),
            "platforms": platforms}


@router.post("/connections", status_code=201)
async def connect_account(payload: ConnectAdAccount, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    await require_manage_integrations(db, project, kind, user)
    try:
        internal = marketing.ConnectionCreate(workspace_id=project.workspace_id, project_id=project.id,
                                               platform=payload.platform, name=payload.name,
                                               external_account_id=payload.external_account_id,
                                               access_token=payload.access_token,
                                               client_id=payload.client_id, client_secret=payload.client_secret)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await marketing.create_connection(internal, request, db)


@router.post("/connections/{connection_id}/test")
async def test_account(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await scoped_connection(db, request, connection_id)
    return await marketing.test_connection(connection_id, request, db)


@router.post("/connections/{connection_id}/sync")
async def sync_account(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await scoped_connection(db, request, connection_id)
    return await marketing.sync_connection(connection_id, request, db)


@router.put("/connections/{connection_id}/token")
async def replace_token(connection_id: int, payload: marketing.ConnectionTokenUpdate, request: Request,
                        db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await scoped_connection(db, request, connection_id)
    return await marketing.update_connection_token(connection_id, payload, request, db)


@router.delete("/connections/{connection_id}", status_code=204)
async def disconnect_account(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await scoped_connection(db, request, connection_id)
    return await marketing.remove_connection(connection_id, request, db)


@router.get("/connections/{connection_id}/vk-campaigns")
async def vk_campaigns(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    row, project = await readable_vk_connection(db, request, connection_id)
    try:
        token = await verified_vk_token(db, row)
        campaigns = await run_in_threadpool(marketing._vk_campaigns, token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"project_id": project.id, "connection_id": row.id, "connection_name": row.name,
            "campaigns": campaigns}


@router.get("/connections/{connection_id}/vk-campaigns/{campaign_id}")
async def vk_campaign_detail(connection_id: int, campaign_id: int, request: Request,
                             db: AsyncSession = Depends(get_db)):
    if campaign_id <= 0:
        raise HTTPException(404, "Кампания VK не найдена")
    row, project = await readable_vk_connection(db, request, connection_id)
    try:
        token = await verified_vk_token(db, row)
        detail = await run_in_threadpool(marketing._vk_campaign_detail, token, campaign_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"project_id": project.id, "connection_id": row.id, "connection_name": row.name, **detail}
