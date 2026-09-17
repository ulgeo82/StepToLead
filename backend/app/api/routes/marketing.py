import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.access import check_origin
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import get_db
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesis, AdHypothesisCampaign,
                                  AdMetricDaily, ClientLeadAttribution, ClientWorkspace, LeadInboundReceipt,
                                  LeadInboundSource)

router = APIRouter(prefix="/marketing", tags=["marketing"])
SUPPORTED = {"meta": "Meta Ads", "yandex": "Яндекс Директ"}


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=180)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str):
        return value.strip()


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    platform: str
    name: str = Field(min_length=2, max_length=180)
    external_account_id: str = Field(min_length=1, max_length=180)
    access_token: str = Field(min_length=10, max_length=4096)

    @field_validator("platform")
    @classmethod
    def supported_platform(cls, value: str):
        value = value.strip().lower()
        if value not in SUPPORTED:
            raise ValueError("Сейчас доступны Meta Ads и Яндекс Директ")
        return value

    @field_validator("name", "external_account_id", "access_token")
    @classmethod
    def trim_values(cls, value: str):
        return value.strip()


class ConnectionTokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=10, max_length=4096)

    @field_validator("access_token")
    @classmethod
    def trim_token(cls, value: str):
        return value.strip()


class HypothesisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=220)
    audience: str | None = Field(default=None, max_length=4000)
    offer: str | None = Field(default=None, max_length=4000)
    landing_url: str | None = Field(default=None, max_length=1000)


class CampaignAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: int = Field(gt=0)
    external_campaign_id: str = Field(min_length=1, max_length=180)


def _request_full(url: str, *, method: str = "GET", headers: dict | None = None,
                  payload: dict | None = None) -> tuple[int, str, dict[str, str]]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "StepToLead/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response.read().decode("utf-8", errors="replace"), response_headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(body).get("error", {})
            if str(error.get("error_code")) == "53":
                raise ValueError("Яндекс отклонил OAuth-токен. Вставьте значение access_token, а не ID приложения, секрет или код подтверждения.") from exc
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ValueError(f"API вернул {exc.code}: {body[:700]}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Не удалось подключиться к API: {exc.reason}") from exc


def _request(url: str, *, method: str = "GET", headers: dict | None = None, payload: dict | None = None) -> tuple[int, str]:
    status, body, _ = _request_full(url, method=method, headers=headers, payload=payload)
    return status, body


def _test_connection(connection: AdConnection) -> dict:
    token = decrypt_secret(connection.access_token_encrypted)
    if connection.platform == "meta":
        account_id = connection.external_account_id.removeprefix("act_")
        query = urllib.parse.urlencode({"fields": "id,name,account_status,currency,timezone_name", "access_token": token})
        _, body = _request(f"https://graph.facebook.com/v22.0/act_{account_id}?{query}")
        data = json.loads(body)
        return {"name": data.get("name"), "currency": data.get("currency"), "remote_status": data.get("account_status")}
    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "ru", "Content-Type": "application/json"}
    if connection.external_account_id:
        headers["Client-Login"] = connection.external_account_id
    _, body = _request("https://api.direct.yandex.com/json/v5/clients", method="POST", headers=headers,
                       payload={"method": "get", "params": {"FieldNames": ["Login", "ClientInfo", "Currency"]}})
    data = json.loads(body)
    clients = data.get("result", {}).get("Clients", [])
    if not clients:
        raise ValueError("API не вернул доступных клиентов. Проверьте логин кабинета и права токена.")
    client = clients[0]
    return {"name": client.get("ClientInfo") or client.get("Login"), "currency": client.get("Currency"), "remote_status": "active"}


def _meta_metrics(connection: AdConnection) -> list[dict]:
    token = decrypt_secret(connection.access_token_encrypted)
    account_id = connection.external_account_id.removeprefix("act_")
    query = urllib.parse.urlencode({
        "fields": "date_start,campaign_id,campaign_name,spend,impressions,clicks", "level": "campaign",
        "date_preset": "maximum", "time_increment": "1", "limit": "500", "access_token": token,
    })
    next_url = f"https://graph.facebook.com/v22.0/act_{account_id}/insights?{query}"
    result: dict[date, dict] = {}
    for _ in range(1000):
        _, body = _request(next_url)
        response = json.loads(body)
        for row in response.get("data", []):
            day = date.fromisoformat(row["date_start"])
            daily = result.setdefault(day, {"date": day, "spend": Decimal("0"), "impressions": 0,
                                            "clicks": 0, "leads": 0, "raw": {}, "campaigns": []})
            spend = Decimal(row.get("spend", "0")); impressions = int(row.get("impressions", 0)); clicks = int(row.get("clicks", 0))
            daily["spend"] += spend; daily["impressions"] += impressions; daily["clicks"] += clicks
            daily["campaigns"].append({"date": day, "external_campaign_id": str(row.get("campaign_id", "unknown")),
                                        "campaign_name": row.get("campaign_name") or "Кампания без названия",
                                        "spend": spend, "impressions": impressions, "clicks": clicks, "raw": row})
        next_url = response.get("paging", {}).get("next")
        if not next_url:
            break
        parsed = urllib.parse.urlparse(next_url)
        if parsed.scheme != "https" or parsed.hostname != "graph.facebook.com":
            raise ValueError("Meta вернула небезопасный адрес следующей страницы отчёта")
    else:
        raise ValueError("Meta вернула слишком много страниц отчёта. Попробуйте синхронизацию ещё раз.")
    return [result[day] for day in sorted(result)]


def _yandex_metrics(connection: AdConnection) -> list[dict]:
    token = decrypt_secret(connection.access_token_encrypted)
    headers = {
        "Authorization": f"Bearer {token}", "Client-Login": connection.external_account_id,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8",
        "processingMode": "auto", "returnMoneyInMicros": "false", "skipReportHeader": "true",
        "skipReportSummary": "true", "skipColumnHeader": "false",
    }
    payload = {"params": {"SelectionCriteria": {},
                          "FieldNames": ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
                          "ReportName": f"StepToLead-{connection.id}-{int(datetime.now().timestamp())}", "ReportType": "CUSTOM_REPORT",
                          "DateRangeType": "ALL_TIME", "Format": "TSV", "IncludeVAT": "YES", "IncludeDiscount": "NO"}}
    body = ""
    for _ in range(40):
        status, body, response_headers = _request_full(
            "https://api.direct.yandex.com/json/v5/reports", method="POST", headers=headers, payload=payload,
        )
        if status == 200:
            break
        if status not in {201, 202}:
            raise ValueError(f"Яндекс вернул неожиданный статус отчёта: {status}")
        retry_value = response_headers.get("retryin") or response_headers.get("retry-after") or "2"
        try:
            retry_seconds = int(float(retry_value))
        except ValueError:
            retry_seconds = 2
        time.sleep(min(max(retry_seconds, 1), 5))
    else:
        raise ValueError("Яндекс ещё формирует полный отчёт. Повторите синхронизацию через несколько минут.")
    result: dict[date, dict] = {}
    for row in csv.DictReader(io.StringIO(body), delimiter="\t"):
        day = date.fromisoformat(row["Date"]); spend = Decimal(row.get("Cost", "0") or "0")
        impressions = int(float(row.get("Impressions", 0) or 0)); clicks = int(float(row.get("Clicks", 0) or 0))
        daily = result.setdefault(day, {"date": day, "spend": Decimal("0"), "impressions": 0,
                                        "clicks": 0, "leads": 0, "raw": {}, "campaigns": []})
        daily["spend"] += spend; daily["impressions"] += impressions; daily["clicks"] += clicks
        daily["campaigns"].append({"date": day, "external_campaign_id": str(row.get("CampaignId", "unknown")),
                                    "campaign_name": row.get("CampaignName") or "Кампания без названия",
                                    "spend": spend, "impressions": impressions, "clicks": clicks, "raw": row})
    return [result[day] for day in sorted(result)]


def serialize_connection(row: AdConnection, workspace_name: str | None = None):
    return {"id": row.id, "workspace_id": row.workspace_id, "workspace_name": workspace_name, "platform": row.platform,
            "platform_name": SUPPORTED.get(row.platform, row.platform), "name": row.name,
            "external_account_id": row.external_account_id, "status": row.status, "currency": row.currency,
            "last_error": row.last_error, "last_checked_at": row.last_checked_at, "last_synced_at": row.last_synced_at,
            "created_at": row.created_at}


@router.get("/workspaces")
async def workspaces(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(ClientWorkspace).order_by(ClientWorkspace.name))).all()


@router.post("/workspaces", status_code=201)
async def create_workspace(payload: WorkspaceCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if await db.scalar(select(ClientWorkspace.id).where(func.lower(ClientWorkspace.name) == payload.name.lower())):
        raise HTTPException(409, "Клиент с таким названием уже существует")
    row = ClientWorkspace(name=payload.name)
    db.add(row); await db.commit(); await db.refresh(row)
    return row


@router.get("/workspaces/{workspace_id}/analysis")
async def workspace_analysis(workspace_id: int, db: AsyncSession = Depends(get_db)):
    workspace = await db.get(ClientWorkspace, workspace_id)
    if not workspace:
        raise HTTPException(404, "Клиент не найден")
    connection_rows = (await db.scalars(select(AdConnection).where(AdConnection.workspace_id == workspace_id)
                                         .order_by(AdConnection.name))).all()
    connection_ids = [row.id for row in connection_rows]
    campaign_rows = []
    if connection_ids:
        campaign_rows = (await db.execute(
            select(AdCampaignMetricDaily.connection_id, AdCampaignMetricDaily.external_campaign_id,
                   func.max(AdCampaignMetricDaily.campaign_name), func.sum(AdCampaignMetricDaily.spend),
                   func.sum(AdCampaignMetricDaily.impressions), func.sum(AdCampaignMetricDaily.clicks),
                   func.min(AdCampaignMetricDaily.date), func.max(AdCampaignMetricDaily.date))
            .where(AdCampaignMetricDaily.connection_id.in_(connection_ids))
            .group_by(AdCampaignMetricDaily.connection_id, AdCampaignMetricDaily.external_campaign_id)
        )).all()
    assignments = (await db.scalars(select(AdHypothesisCampaign).where(
        AdHypothesisCampaign.connection_id.in_(connection_ids)
    ))).all() if connection_ids else []
    assignment_map = {(row.connection_id, row.external_campaign_id): row.hypothesis_id for row in assignments}
    attribution_rows = (await db.execute(
        select(ClientLeadAttribution.hypothesis_id, ClientLeadAttribution.connection_id,
               ClientLeadAttribution.external_campaign_id,
               func.count(func.distinct(ClientLeadAttribution.lead_id)))
        .join(LeadInboundSource, LeadInboundSource.id == ClientLeadAttribution.source_id)
        .where(LeadInboundSource.workspace_id == workspace_id)
        .group_by(ClientLeadAttribution.hypothesis_id, ClientLeadAttribution.connection_id,
                  ClientLeadAttribution.external_campaign_id)
    )).all()
    campaign_applications = {(connection_id, campaign_id): int(count) for _, connection_id, campaign_id, count in attribution_rows
                             if connection_id is not None and campaign_id is not None}
    hypothesis_applications = {hypothesis_id: 0 for hypothesis_id in [row.id for row in (await db.scalars(
        select(AdHypothesis).where(AdHypothesis.workspace_id == workspace_id)
    )).all()]}
    for hypothesis_id, _, _, count in attribution_rows:
        if hypothesis_id is not None:
            hypothesis_applications[hypothesis_id] = hypothesis_applications.get(hypothesis_id, 0) + int(count)
    connection_map = {row.id: row for row in connection_rows}
    campaigns = []
    for connection_id, external_id, name, spend, impressions, clicks, first_date, last_date in campaign_rows:
        spend_value, clicks_value = float(spend or 0), int(clicks or 0)
        campaigns.append({"connection_id": connection_id, "connection_name": connection_map[connection_id].name,
                          "platform": connection_map[connection_id].platform, "external_campaign_id": external_id,
                          "name": name, "spend": spend_value, "impressions": int(impressions or 0),
                          "clicks": clicks_value, "cpc": spend_value / clicks_value if clicks_value else None,
                          "applications": campaign_applications.get((connection_id, external_id), 0),
                          "first_date": first_date, "last_date": last_date,
                          "hypothesis_id": assignment_map.get((connection_id, external_id))})
    hypothesis_rows = (await db.scalars(select(AdHypothesis).where(AdHypothesis.workspace_id == workspace_id)
                                        .order_by(AdHypothesis.created_at.desc()))).all()
    hypotheses = []
    for hypothesis in hypothesis_rows:
        nested = [campaign for campaign in campaigns if campaign["hypothesis_id"] == hypothesis.id]
        spend = sum(campaign["spend"] for campaign in nested); clicks = sum(campaign["clicks"] for campaign in nested)
        applications = hypothesis_applications.get(hypothesis.id, 0)
        hypotheses.append({"id": hypothesis.id, "name": hypothesis.name, "audience": hypothesis.audience,
                           "offer": hypothesis.offer, "landing_url": hypothesis.landing_url, "status": hypothesis.status,
                           "campaigns": nested, "spend": spend, "clicks": clicks,
                           "impressions": sum(campaign["impressions"] for campaign in nested),
                           "applications": applications, "cpc": spend / clicks if clicks else None,
                           "conversion_rate": applications / clicks * 100 if clicks else None,
                           "cost_per_application": spend / applications if applications else None})
    total_applications = sum(int(row[3]) for row in attribution_rows)
    attributed_applications = sum(hypothesis_applications.values())
    return {"workspace": {"id": workspace.id, "name": workspace.name},
            "connections": [serialize_connection(row, workspace.name) for row in connection_rows],
            "hypotheses": hypotheses,
            "applications": total_applications, "unattributed_applications": total_applications - attributed_applications,
            "unassigned_campaigns": [campaign for campaign in campaigns if campaign["hypothesis_id"] is None]}


@router.post("/hypotheses", status_code=201)
async def create_hypothesis(payload: HypothesisCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(ClientWorkspace, payload.workspace_id):
        raise HTTPException(404, "Клиент не найден")
    row = AdHypothesis(**payload.model_dump())
    db.add(row); await db.commit(); await db.refresh(row)
    return row


@router.post("/hypotheses/{hypothesis_id}/campaigns")
async def assign_campaign(hypothesis_id: int, payload: CampaignAssignment, request: Request,
                          db: AsyncSession = Depends(get_db)):
    check_origin(request)
    hypothesis = await db.get(AdHypothesis, hypothesis_id)
    connection = await db.get(AdConnection, payload.connection_id)
    if not hypothesis or not connection or hypothesis.workspace_id != connection.workspace_id:
        raise HTTPException(404, "Гипотеза или рекламный кабинет не найдены")
    campaign_exists = await db.scalar(select(AdCampaignMetricDaily.id).where(
        AdCampaignMetricDaily.connection_id == payload.connection_id,
        AdCampaignMetricDaily.external_campaign_id == payload.external_campaign_id,
    ))
    if not campaign_exists:
        raise HTTPException(404, "Кампания ещё не загружена из рекламного кабинета")
    await db.execute(delete(AdHypothesisCampaign).where(
        AdHypothesisCampaign.connection_id == payload.connection_id,
        AdHypothesisCampaign.external_campaign_id == payload.external_campaign_id,
    ))
    db.add(AdHypothesisCampaign(hypothesis_id=hypothesis_id, **payload.model_dump()))
    source_ids = select(LeadInboundSource.id).where(LeadInboundSource.workspace_id == hypothesis.workspace_id)
    await db.execute(update(ClientLeadAttribution).where(
        ClientLeadAttribution.source_id.in_(source_ids),
        ClientLeadAttribution.external_campaign_id == payload.external_campaign_id,
    ).values(hypothesis_id=hypothesis_id, connection_id=payload.connection_id))
    await db.commit()
    return {"ok": True}


@router.get("/connections")
async def connections(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AdConnection, ClientWorkspace.name).join(ClientWorkspace).order_by(AdConnection.created_at.desc()))).all()
    return [serialize_connection(connection, name) for connection, name in rows]


@router.post("/connections", status_code=201)
async def create_connection(payload: ConnectionCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(ClientWorkspace, payload.workspace_id):
        raise HTTPException(404, "Клиент не найден")
    row = AdConnection(workspace_id=payload.workspace_id, platform=payload.platform, name=payload.name,
                       external_account_id=payload.external_account_id, access_token_encrypted=encrypt_secret(payload.access_token), status="pending")
    db.add(row); await db.commit(); await db.refresh(row)
    return serialize_connection(row)


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AdConnection, connection_id)
    if not row: raise HTTPException(404, "Подключение не найдено")
    try:
        result = await run_in_threadpool(_test_connection, row)
        row.status = "connected"; row.currency = result.get("currency"); row.last_error = None
    except Exception as exc:
        row.status = "error"; row.last_error = str(exc)[:1500]; result = None
    row.last_checked_at = datetime.now(timezone.utc); await db.commit()
    if result is None: raise HTTPException(422, row.last_error)
    return {"ok": True, "details": result}


@router.put("/connections/{connection_id}/token")
async def update_connection_token(connection_id: int, payload: ConnectionTokenUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AdConnection, connection_id)
    if not row:
        raise HTTPException(404, "Подключение не найдено")
    row.access_token_encrypted = encrypt_secret(payload.access_token)
    row.status = "pending"
    row.last_error = None
    row.last_checked_at = None
    await db.commit()
    return {"ok": True}


@router.post("/connections/{connection_id}/sync")
async def sync_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AdConnection, connection_id)
    if not row: raise HTTPException(404, "Подключение не найдено")
    try:
        metrics = await run_in_threadpool(_meta_metrics if row.platform == "meta" else _yandex_metrics, row)
        for item in metrics:
            campaigns = item.pop("campaigns", [])
            existing = await db.scalar(select(AdMetricDaily).where(AdMetricDaily.connection_id == row.id, AdMetricDaily.date == item["date"]))
            if existing:
                for key in ("spend", "impressions", "clicks", "leads", "raw"): setattr(existing, key, item[key])
            else: db.add(AdMetricDaily(connection_id=row.id, **item))
            for campaign in campaigns:
                campaign_row = await db.scalar(select(AdCampaignMetricDaily).where(
                    AdCampaignMetricDaily.connection_id == row.id,
                    AdCampaignMetricDaily.date == campaign["date"],
                    AdCampaignMetricDaily.external_campaign_id == campaign["external_campaign_id"],
                ))
                if campaign_row:
                    for key in ("campaign_name", "spend", "impressions", "clicks", "raw"):
                        setattr(campaign_row, key, campaign[key])
                else:
                    db.add(AdCampaignMetricDaily(connection_id=row.id, **campaign))
        row.status = "connected"; row.last_error = None; row.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        row.status = "error"; row.last_error = str(exc)[:1500]; await db.commit()
        raise HTTPException(422, row.last_error)
    metric_dates = [item["date"] for item in metrics]
    return {"ok": True, "days": len(metrics),
            "first_date": min(metric_dates).isoformat() if metric_dates else None,
            "last_date": max(metric_dates).isoformat() if metric_dates else None}


@router.delete("/connections/{connection_id}", status_code=204)
async def remove_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(AdConnection, connection_id): raise HTTPException(404, "Подключение не найдено")
    await db.execute(delete(AdConnection).where(AdConnection.id == connection_id)); await db.commit()


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    totals = (await db.execute(select(func.coalesce(func.sum(AdMetricDaily.spend), 0), func.coalesce(func.sum(AdMetricDaily.impressions), 0),
                                      func.coalesce(func.sum(AdMetricDaily.clicks), 0), func.coalesce(func.sum(AdMetricDaily.leads), 0),
                                      func.min(AdMetricDaily.date), func.max(AdMetricDaily.date),
                                      func.count(func.distinct(AdMetricDaily.date))))).one()
    spend, clicks = float(totals[0]), int(totals[2])
    applications = await db.scalar(select(func.count(func.distinct(LeadInboundReceipt.lead_id)))) or 0
    return {"spend": spend, "impressions": int(totals[1]), "clicks": clicks,
            "applications": applications, "cpc": spend / clicks if clicks else None,
            "conversion_rate": applications / clicks * 100 if clicks else None,
            "cost_per_application": spend / applications if applications else None,
            "first_date": totals[4], "last_date": totals[5], "days": int(totals[6]),
            "connections": await db.scalar(select(func.count(AdConnection.id))) or 0,
            "workspaces": await db.scalar(select(func.count(ClientWorkspace.id))) or 0}
