import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.access import check_origin
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import get_db
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesis, AdHypothesisCampaign,
                                  AdMetricDaily, ClientLeadAttribution, ClientWorkspace, LeadInboundReceipt,
                                  LeadInboundSource, Project)
from app.services import avito
from app.services.project_scope import default_project

router = APIRouter(prefix="/marketing", tags=["marketing"])
SUPPORTED = {"meta": "Meta Ads", "yandex": "Яндекс Директ", "vk_ads": "VK Реклама",
             "avito_items": "Авито · Объявления", "avito_ads": "Авито Реклама",
             "yandex_maps": "Яндекс Карты", "2gis": "2ГИС"}
# Platforms authorised with Client ID + Client Secret (stored in the generic vk_* OAuth columns).
CLIENT_CREDENTIAL_PLATFORMS = {"vk_ads", "avito_items", "avito_ads"}


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
    project_id: int | None = Field(default=None, gt=0)
    platform: str
    name: str = Field(min_length=2, max_length=180)
    external_account_id: str = Field(default="", max_length=180)
    access_token: str | None = Field(default=None, min_length=10, max_length=4096)
    client_id: str | None = Field(default=None, min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, min_length=10, max_length=4096)

    @field_validator("platform")
    @classmethod
    def supported_platform(cls, value: str):
        value = value.strip().lower()
        if value not in SUPPORTED:
            raise ValueError("Платформа не поддерживается")
        return value

    @field_validator("name", "external_account_id")
    @classmethod
    def trim_values(cls, value: str):
        return value.strip()

    @model_validator(mode="after")
    def credentials_for_platform(self):
        if self.platform == "vk_ads":
            if not self.client_id or not self.client_secret:
                raise ValueError("Для VK Рекламы нужны Client ID и Client Secret из настроек доступа к API")
        elif self.platform == "avito_items":
            if not self.client_id or not self.client_secret:
                raise ValueError("Для Авито нужны Client ID и Client Secret: avito.ru/professionals/api")
        elif self.platform == "avito_ads":
            if not self.client_id or not self.client_secret or not self.external_account_id.isdigit():
                raise ValueError("Для Авито Рекламы нужны ID аккаунта (число) и ключи из вкладки «API» кабинета")
        elif self.platform in {"yandex_maps", "2gis"}:
            pass  # no API: spend, statistics files and call tracking (services/maps.py)
        elif not self.external_account_id or not self.access_token:
            raise ValueError("Укажите ID кабинета и OAuth access token")
        return self


class ConnectionTokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str | None = Field(default=None, min_length=10, max_length=4096)
    client_id: str | None = Field(default=None, min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, min_length=10, max_length=4096)

    @field_validator("access_token")
    @classmethod
    def trim_token(cls, value: str | None):
        return value.strip() if value else value


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


VK_API = "https://ads.vk.ru"


def _vk_oauth(fields: dict[str, str]) -> dict:
    """VK OAuth uses form encoding; never include credentials or response bodies in errors."""
    request = urllib.request.Request(
        f"{VK_API}/api/v2/oauth2/token.json",
        data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "StepToLead/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"VK отклонил авторизацию (HTTP {exc.code}). Проверьте API-доступ и реквизиты.") from None
    except urllib.error.URLError as exc:
        raise ValueError("VK API недоступен. Повторите проверку позже.") from None
    if not isinstance(result, dict) or not result.get("access_token"):
        raise ValueError("VK не вернул access token")
    return result


def _vk_json(path: str, token: str, params: dict | None = None) -> dict:
    url = f"{VK_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       "User-Agent": "StepToLead/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"VK API вернул HTTP {exc.code}. Проверьте доступ к выбранному кабинету.") from None
    except urllib.error.URLError:
        raise ValueError("VK API недоступен. Повторите синхронизацию позже.") from None
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("VK API не вернул запрошенные данные")
    return result


async def _vk_access_token(db: AsyncSession, row: AdConnection) -> str:
    now = datetime.now(timezone.utc)
    expiry = row.vk_access_expires_at
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    token = decrypt_secret(row.access_token_encrypted)
    if token and token != "pending" and expiry and expiry > now + timedelta(minutes=5):
        return token
    client_id = decrypt_secret(row.vk_client_id_encrypted)
    client_secret = decrypt_secret(row.vk_client_secret_encrypted)
    if not client_id or not client_secret:
        raise ValueError("Для VK подключения не сохранены Client ID и Client Secret")
    refresh = decrypt_secret(row.vk_refresh_token_encrypted)
    fields = ({"grant_type": "refresh_token", "refresh_token": refresh,
               "client_id": client_id, "client_secret": client_secret} if refresh else
              {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    try:
        data = await run_in_threadpool(_vk_oauth, fields)
    except ValueError:
        if not refresh:
            raise
        data = await run_in_threadpool(_vk_oauth, {"grant_type": "client_credentials",
                                                    "client_id": client_id, "client_secret": client_secret})
    row.access_token_encrypted = encrypt_secret(data["access_token"])
    if data.get("refresh_token"):
        row.vk_refresh_token_encrypted = encrypt_secret(data["refresh_token"])
    row.vk_access_expires_at = now + timedelta(seconds=max(int(data.get("expires_in") or 86400), 60))
    await db.commit()
    return data["access_token"]


def _vk_metrics(token: str) -> list[dict]:
    """Import only VK's ad facts. VK goals are not CRM leads."""
    campaigns = []
    for offset in range(0, 10000, 100):
        page = _vk_json("/api/v2/ad_plans.json", token, {"limit": 100, "offset": offset})
        items = page.get("items", [])
        if not isinstance(items, list):
            raise ValueError("VK вернул некорректный список кампаний")
        campaigns.extend(items)
        if len(items) < 100:
            break
    else:
        raise ValueError("Слишком много кампаний VK для одной синхронизации")
    result: dict[date, dict] = {}
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=365)  # VK limits statistics to the last 366 days.
    names = {str(item["id"]): item.get("name") or "Кампания без названия" for item in campaigns if item.get("id") is not None}
    ids = list(names)
    for index in range(0, len(ids), 200):
        report = _vk_json("/api/v2/statistics/ad_plans/day.json", token,
                          {"date_from": start.isoformat(), "date_to": today.isoformat(),
                           "id": ",".join(ids[index:index + 200]), "metrics": "base"})
        for campaign in report.get("items", []):
            campaign_id = str(campaign.get("id"))
            for point in campaign.get("rows", []):
                day = date.fromisoformat(point["date"])
                base = point.get("base") or point.get("metrics", {}).get("base") or {}
                spend = Decimal(str(base.get("spent") or "0"))
                impressions = int(base.get("shows") or 0)
                clicks = int(base.get("clicks") or 0)
                daily = result.setdefault(day, {"date": day, "spend": Decimal("0"), "impressions": 0,
                                                "clicks": 0, "leads": 0, "raw": {}, "campaigns": []})
                daily["spend"] += spend
                daily["impressions"] += impressions
                daily["clicks"] += clicks
                daily["campaigns"].append({"date": day, "external_campaign_id": campaign_id,
                                           "campaign_name": names.get(campaign_id, "Кампания без названия"),
                                           "spend": spend, "impressions": impressions, "clicks": clicks,
                                           "raw": point})
    return [result[day] for day in sorted(result)]


def _vk_campaigns(token: str) -> list[dict]:
    """Read campaign metadata independently of statistics (including zero-delivery campaigns)."""
    campaigns = []
    for offset in range(0, 10000, 100):
        page = _vk_json("/api/v2/ad_plans.json", token,
                        {"limit": 100, "offset": offset, "fields": "id,name,status,vkads_status,objective,budget_limit_day"})
        items = page.get("items", [])
        if not isinstance(items, list):
            raise ValueError("VK вернул некорректный список кампаний")
        campaigns.extend({"id": str(item["id"]), "name": item.get("name") or "Кампания без названия",
                          "status": item.get("status"), "delivery": item.get("vkads_status"),
                          "objective": item.get("objective"), "budget_limit_day": item.get("budget_limit_day")}
                         for item in items if item.get("id") is not None)
        if len(items) < 100:
            return campaigns
    raise ValueError("Слишком много кампаний VK для одного кабинета")


def _vk_campaign_detail(token: str, campaign_id: int) -> dict:
    """Read a campaign and its groups/ads without changing anything in VK."""
    campaign_fields = ("id,name,status,vkads_status,created,updated,autobidding_mode,"
                       "budget_limit,budget_limit_day,date_start,date_end,max_price,objective,priced_goal")
    campaign = _vk_json(f"/api/v2/ad_plans/{campaign_id}.json", token, {"fields": campaign_fields})
    if str(campaign.get("id")) != str(campaign_id):
        raise ValueError("VK не вернул выбранную кампанию")
    groups = []
    for offset in range(0, 10000, 100):
        page = _vk_json("/api/v2/ad_groups.json", token,
                        {"limit": 100, "offset": offset, "fields": "id,ad_plan_id,name,status"})
        items = page.get("items", [])
        if not isinstance(items, list):
            raise ValueError("VK вернул некорректный список групп")
        groups.extend(item for item in items if str(item.get("ad_plan_id")) == str(campaign_id))
        if len(groups) > 50:
            raise ValueError("В кампании больше 50 групп. Уточните кампанию в кабинете VK")
        if len(items) < 100:
            break
    else:
        raise ValueError("Слишком много групп VK для одного кабинета")
    group_fields = ("id,ad_plan_id,name,status,delivery,issues,package_id,objective,"
                    "budget_limit,budget_limit_day,date_start,date_end,max_price,price,"
                    "age_restrictions,enable_utm,utm,targetings,created,updated")
    detailed_groups = [_vk_json(f"/api/v2/ad_groups/{int(group['id'])}.json", token,
                                {"fields": group_fields}) for group in groups]
    banners = []
    group_ids = [str(group["id"]) for group in detailed_groups]
    if group_ids:
        for offset in range(0, 1000, 100):
            page = _vk_json("/api/v2/banners.json", token,
                            {"limit": 100, "offset": offset, "_ad_group_id__in": ",".join(group_ids),
                             "fields": "id,ad_group_id,name,status,delivery,issues,moderation_status,textblocks,urls"})
            items = page.get("items", [])
            if not isinstance(items, list):
                raise ValueError("VK вернул некорректный список объявлений")
            banners.extend(item for item in items if str(item.get("ad_group_id")) in group_ids)
            if len(banners) > 100:
                raise ValueError("В кампании больше 100 объявлений. Уточните их в кабинете VK")
            if len(items) < 100:
                break
        else:
            raise ValueError("Слишком много объявлений VK для одной кампании")
    return {"campaign": campaign,
            "groups": [{**group, "banners": [banner for banner in banners
                                                if str(banner.get("ad_group_id")) == str(group.get("id"))]}
                       for group in detailed_groups]}


def _test_connection(connection: AdConnection) -> dict:
    if connection.platform == "vk_ads":
        raise ValueError("VK подключение проверяется с обновлением OAuth-токена")
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
async def workspaces(deleted: bool = False, db: AsyncSession = Depends(get_db)):
    query = select(ClientWorkspace).order_by(ClientWorkspace.name)
    query = query.where(ClientWorkspace.status == "deleted") if deleted else query.where(ClientWorkspace.status != "deleted")
    return (await db.scalars(query)).all()


class WorkspaceDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_name: str = Field(min_length=1, max_length=180)


@router.post("/workspaces/{workspace_id}/delete")
async def delete_workspace(workspace_id: int, payload: WorkspaceDelete, request: Request, db: AsyncSession = Depends(get_db)):
    """Removes the company from the portal and switches everything off; history is kept and can be restored."""
    from app.services import workspace_archive
    check_origin(request)
    row = await db.get(ClientWorkspace, workspace_id)
    if not row or row.status == "deleted":
        raise HTTPException(404, "Клиент не найден")
    if payload.confirm_name.strip().casefold() != row.name.strip().casefold():
        raise HTTPException(422, "Название не совпадает — введите название компании точно как в карточке")
    result = await workspace_archive.delete_workspace(db, row)
    await db.commit()
    return {"ok": True, **result}


@router.post("/workspaces/{workspace_id}/restore")
async def restore_workspace(workspace_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services import workspace_archive
    check_origin(request)
    row = await db.get(ClientWorkspace, workspace_id)
    if not row or row.status != "deleted":
        raise HTTPException(404, "Удалённый клиент не найден")
    try:
        await workspace_archive.restore_workspace(db, row)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await db.commit()
    return row


@router.post("/workspaces", status_code=201)
async def create_workspace(payload: WorkspaceCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if await db.scalar(select(ClientWorkspace.id).where(func.lower(ClientWorkspace.name) == payload.name.lower())):
        raise HTTPException(409, "Клиент с таким названием уже существует")
    row = ClientWorkspace(name=payload.name)
    db.add(row); await db.flush()
    await default_project(db, row.id)
    await db.commit(); await db.refresh(row)
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
    project = await default_project(db, payload.workspace_id)
    row = AdHypothesis(**payload.model_dump(), project_id=project.id)
    db.add(row); await db.commit(); await db.refresh(row)
    return row


@router.post("/hypotheses/{hypothesis_id}/campaigns")
async def assign_campaign(hypothesis_id: int, payload: CampaignAssignment, request: Request,
                          db: AsyncSession = Depends(get_db)):
    check_origin(request)
    hypothesis = await db.get(AdHypothesis, hypothesis_id)
    connection = await db.get(AdConnection, payload.connection_id)
    if not hypothesis or not connection or hypothesis.project_id != connection.project_id:
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
    source_ids = select(LeadInboundSource.id).where(LeadInboundSource.project_id == hypothesis.project_id)
    await db.execute(update(ClientLeadAttribution).where(
        ClientLeadAttribution.source_id.in_(source_ids),
        ClientLeadAttribution.connection_id == payload.connection_id,
        ClientLeadAttribution.external_campaign_id == payload.external_campaign_id,
    ).values(hypothesis_id=hypothesis_id))
    await db.commit()
    return {"ok": True}


@router.get("/connections")
async def connections(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AdConnection, ClientWorkspace.name).join(ClientWorkspace)
                             .where(AdConnection.platform != "telegram_ads")
                             .order_by(AdConnection.created_at.desc()))).all()
    return [serialize_connection(connection, name) for connection, name in rows]


@router.post("/connections", status_code=201)
async def create_connection(payload: ConnectionCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(ClientWorkspace, payload.workspace_id):
        raise HTTPException(404, "Клиент не найден")
    project = await (db.get(Project, payload.project_id) if payload.project_id else default_project(db, payload.workspace_id))
    if not project or project.workspace_id != payload.workspace_id:
        raise HTTPException(404, "Проект клиента не найден")
    row = AdConnection(workspace_id=payload.workspace_id, project_id=project.id, platform=payload.platform, name=payload.name,
                       external_account_id=payload.external_account_id or "pending",
                       access_token_encrypted=encrypt_secret(payload.access_token or "pending"), status="pending",
                       vk_client_id_encrypted=encrypt_secret(payload.client_id) if payload.platform in CLIENT_CREDENTIAL_PLATFORMS else None,
                       vk_client_secret_encrypted=encrypt_secret(payload.client_secret) if payload.platform in CLIENT_CREDENTIAL_PLATFORMS else None)
    db.add(row); await db.commit(); await db.refresh(row)
    return serialize_connection(row)


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AdConnection, connection_id)
    if not row: raise HTTPException(404, "Подключение не найдено")
    try:
        if row.platform in {"yandex_maps", "2gis"}:
            result = {"currency": "RUB"}
        elif row.platform == "vk_ads":
            token = await _vk_access_token(db, row)
            data = await run_in_threadpool(_vk_json, "/api/v3/user.json", token)
            account_id = str(data.get("id") or "")
            if not account_id:
                raise ValueError("VK не вернул идентификатор кабинета")
            if row.external_account_id not in {"pending", account_id}:
                raise ValueError("Токен VK принадлежит другому рекламному кабинету")
            row.external_account_id = account_id
            result = {"name": data.get("username"), "currency": data.get("currency"),
                      "remote_status": data.get("status")}
        elif row.platform in avito.PLATFORMS:
            result = await avito.test(db, row)
        else:
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
    if row.platform in CLIENT_CREDENTIAL_PLATFORMS:
        if not payload.client_id or not payload.client_secret:
            raise HTTPException(422, "Нужны новые Client ID и Client Secret")
        row.vk_client_id_encrypted = encrypt_secret(payload.client_id)
        row.vk_client_secret_encrypted = encrypt_secret(payload.client_secret)
        row.vk_refresh_token_encrypted = None
        row.vk_access_expires_at = None
        row.access_token_encrypted = encrypt_secret("pending")
    else:
        if not payload.access_token:
            raise HTTPException(422, "Укажите новый access token")
        row.access_token_encrypted = encrypt_secret(payload.access_token)
    row.status = "pending"
    row.last_error = None
    row.last_checked_at = None
    await db.commit()
    return {"ok": True}


async def store_metrics(db: AsyncSession, row: AdConnection, metrics: list[dict]) -> None:
    """Upsert daily account and campaign facts. Does not commit. Pops "campaigns" from each item."""
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


async def sync_core(db: AsyncSession, connection_id: int) -> dict:
    """Pull statistics of one ad cabinet into AdMetricDaily. Commits. Raises ValueError with the reason on failure."""
    row = await db.get(AdConnection, connection_id)
    if not row:
        raise LookupError("Подключение не найдено")
    if row.status == "disconnected":
        raise PermissionError("Сначала восстановите подключение")
    if row.platform in {"yandex_maps", "2gis"}:  # nothing to pull: data comes from files, budgets and call tracking
        row.last_synced_at = datetime.now(timezone.utc); row.status = "connected"; await db.commit()
        return {"ok": True, "days": 0, "first_date": None, "last_date": None}
    row.status = "syncing"
    await db.commit()
    try:
        if row.platform == "vk_ads":
            token = await _vk_access_token(db, row)
            data = await run_in_threadpool(_vk_json, "/api/v3/user.json", token)
            if str(data.get("id")) != row.external_account_id:
                raise ValueError("Токен VK больше не соответствует подключенному кабинету")
            metrics = await run_in_threadpool(_vk_metrics, token)
        elif row.platform == "meta":
            metrics = await run_in_threadpool(_meta_metrics, row)
        elif row.platform in avito.PLATFORMS:
            metrics = await avito.metrics(db, row)
        else:
            metrics = await run_in_threadpool(_yandex_metrics, row)
        await store_metrics(db, row, metrics)
        row.status = "connected"; row.last_error = None; row.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        row = await db.get(AdConnection, connection_id)
        row.status = "error"; row.last_error = str(exc)[:1500]; await db.commit()
        raise ValueError(row.last_error) from exc
    metric_dates = [item["date"] for item in metrics]
    return {"ok": True, "days": len(metrics),
            "first_date": min(metric_dates).isoformat() if metric_dates else None,
            "last_date": max(metric_dates).isoformat() if metric_dates else None}


@router.post("/connections/{connection_id}/sync")
async def sync_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    try:
        return await sync_core(db, connection_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.delete("/connections/{connection_id}", status_code=204)
async def remove_connection(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AdConnection, connection_id)
    if not row: raise HTTPException(404, "Подключение не найдено")
    row.status = "disconnected"
    await db.commit()


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
