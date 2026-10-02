"""Avito integrations: classifieds (Авито · Объявления) and display ads (Авито Реклама).

Both use https://api.avito.ru with OAuth client_credentials (token lives 24h).
Client ID/secret are stored encrypted in the generic OAuth columns of AdConnection
(historically named vk_*), the current access token in access_token_encrypted.

Avito specifics worth remembering:
* stats/v2 (объявления) allows 1 request per minute and 270 days of depth; money
  metrics come in kopecks.
* Avito Реклама statistics: max 100 days per request, each call costs API points
  (campaign stats = 10 points); money fields without "Kopeks" are whole rubles incl. VAT.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.marketing import AdConnection

logger = logging.getLogger("uvicorn.error.avito")

API = "https://api.avito.ru"
ITEMS = "avito_items"
ADS = "avito_ads"
PLATFORMS = {ITEMS, ADS}
ITEM_METRICS = ["impressions", "views", "contacts", "contactsShowPhone", "contactsMessenger", "favorites",
                "allSpending", "spending", "presenceSpending", "promoSpending", "spendingBonus"]
ITEMS_FIRST_SYNC_DAYS = 269   # API depth is 270 days
ADS_FIRST_SYNC_DAYS = 365
RESYNC_DAYS = 30              # Avito corrects recent days; re-read a month every time.
ADS_STATS_CHUNK = 100


class AvitoError(ValueError):
    """User-facing error (Russian text, never contains secrets)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def config(row: AdConnection) -> dict:
    return dict(row.config or {})


def set_config(row: AdConnection, **values) -> dict:
    data = config(row)
    data.update(values)
    row.config = data  # reassign so SQLAlchemy notices the JSON change
    return data


async def _token_request(client_id: str, client_secret: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(f"{API}/token", data={
                "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    except httpx.HTTPError:
        raise AvitoError("API Авито недоступен. Повторите попытку позже.") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("access_token"):
        raise AvitoError("Авито отклонил Client ID / Client Secret. Проверьте ключи и что они выпущены "
                         "в нужном кабинете.", response.status_code)
    return data


async def access_token(db, row: AdConnection, *, force: bool = False) -> str:
    """Cached client_credentials token; commits the refreshed token."""
    expiry = row.vk_access_expires_at
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    token = decrypt_secret(row.access_token_encrypted)
    if not force and token and token != "pending" and expiry and expiry > _now() + timedelta(minutes=10):
        return token
    client_id = decrypt_secret(row.vk_client_id_encrypted)
    client_secret = decrypt_secret(row.vk_client_secret_encrypted)
    if not client_id or not client_secret:
        raise AvitoError("Для подключения Авито не сохранены Client ID и Client Secret")
    data = await _token_request(client_id, client_secret)
    row.access_token_encrypted = encrypt_secret(data["access_token"])
    row.vk_access_expires_at = _now() + timedelta(seconds=max(int(data.get("expires_in") or 86400), 600))
    await db.commit()
    return data["access_token"]


class AvitoClient:
    """Thin async client with Russian error messages and polite 429 handling."""

    def __init__(self, token: str, *, max_wait: float = 8):
        self.token = token
        self.max_wait = max_wait

    async def request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                      allow: tuple[int, ...] = ()) -> tuple[int, dict]:
        delay = 1.0
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=40) as client:
                    response = await client.request(method, f"{API}{path}", json=json, params=params,
                                                     headers={"Authorization": f"Bearer {self.token}",
                                                              "User-Agent": "StepToLead/1.0"})
            except httpx.HTTPError:
                raise AvitoError("API Авито недоступен. Повторите попытку позже.") from None
            if response.status_code == 429 and attempt < 3 and delay <= self.max_wait:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            break
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code in allow:
            return response.status_code, body if isinstance(body, dict) else {"items": body}
        if response.status_code == 429:
            raise AvitoError("Авито ограничил частоту запросов (для статистики объявлений — 1 запрос в минуту). "
                             "Повторите через минуту.", 429)
        if response.status_code == 401:
            raise AvitoError("Токен Авито недействителен. Обновите Client ID и Client Secret.", 401)
        if response.status_code == 403:
            raise AvitoError("У ключей нет доступа к этому разделу Авито (проверьте тариф, права и ID кабинета).", 403)
        if response.status_code == 404:
            raise AvitoError("Авито не нашёл запрошенный кабинет или объект.", 404)
        if response.status_code >= 400:
            message = ""
            if isinstance(body, dict):
                error = body.get("error") if isinstance(body.get("error"), dict) else body
                message = str(error.get("message") or "")[:300]
            raise AvitoError(f"Авито вернул ошибку {response.status_code}{': ' + message if message else ''}",
                             response.status_code)
        return response.status_code, body if isinstance(body, dict) else {"items": body}

    async def get(self, path: str, **kwargs) -> dict:
        return (await self.request("GET", path, **kwargs))[1]

    async def post(self, path: str, payload: dict, **kwargs) -> dict:
        return (await self.request("POST", path, json=payload, **kwargs))[1]


async def client_for(db, row: AdConnection) -> AvitoClient:
    return AvitoClient(await access_token(db, row))


def _day_from_timestamp(value) -> date:
    # Avito day buckets are midnight timestamps (UTC or Moscow); +12h lands inside the right day either way.
    return datetime.fromtimestamp(int(value) + 43200, tz=timezone.utc).date()


def _kopecks(value) -> Decimal:
    return (Decimal(str(value or 0)) / 100).quantize(Decimal("0.01"))


def _sync_window(row: AdConnection, first_days: int) -> tuple[date, date]:
    today = _now().date()
    days = RESYNC_DAYS if row.last_synced_at else first_days
    return today - timedelta(days=days), today


# --------------------------------------------------------------------------- Авито · Объявления

async def items_profile(client: AvitoClient) -> dict:
    data = await client.get("/core/v1/accounts/self")
    if not data.get("id"):
        raise AvitoError("Авито не вернул ID профиля")
    return data


async def items_balance(client: AvitoClient, user_id: str) -> dict | None:
    try:
        return await client.get(f"/core/v1/accounts/{user_id}/balance/")
    except AvitoError as exc:
        logger.info("avito balance unavailable user=%s: %s", user_id, exc)
        return None


async def items_metrics(client: AvitoClient, row: AdConnection) -> list[dict]:
    start, end = _sync_window(row, ITEMS_FIRST_SYNC_DAYS)
    groupings = []
    offset = 0
    while True:
        data = await client.post(f"/stats/v2/accounts/{row.external_account_id}/items", {
            "dateFrom": start.isoformat(), "dateTo": end.isoformat(), "grouping": "day",
            "metrics": ITEM_METRICS, "limit": 1000, "offset": offset})
        result = data.get("result") or {}
        page = result.get("groupings") or []
        groupings.extend(page)
        offset += len(page)
        if not page or offset >= int(result.get("dataTotalCount") or 0):
            break
        await asyncio.sleep(61)  # 1 request/minute on this method; practically never needed (270 < 1000).
    metrics = []
    for group in groupings:
        values = {item.get("slug"): item.get("value") for item in group.get("metrics") or []}
        money = values.get("spending", values.get("allSpending"))
        metrics.append({
            "date": _day_from_timestamp(group["id"]),
            "spend": _kopecks(money),
            "impressions": int(values.get("impressions") or 0),
            "clicks": int(values.get("views") or 0),       # просмотры объявления = переходы в карточку
            "leads": int(values.get("contacts") or 0),     # контакты Авито (не лиды CRM)
            "raw": {key: values.get(key) for key in ITEM_METRICS if key in values},
            "campaigns": [],
        })
    return sorted(metrics, key=lambda item: item["date"])


async def items_listing_stats(client: AvitoClient, user_id: str, start: date, end: date) -> dict[str, dict]:
    """Per-listing totals for a period (one stats call; 1 req/min limit applies)."""
    result: dict[str, dict] = {}
    offset = 0
    while True:
        data = await client.post(f"/stats/v2/accounts/{user_id}/items", {
            "dateFrom": start.isoformat(), "dateTo": end.isoformat(), "grouping": "item",
            "metrics": ITEM_METRICS, "limit": 1000, "offset": offset,
            "sort": {"key": "views", "order": "desc"}})
        payload = data.get("result") or {}
        page = payload.get("groupings") or []
        for group in page:
            values = {item.get("slug"): item.get("value") for item in group.get("metrics") or []}
            spend = _kopecks(values.get("spending", values.get("allSpending")))
            result[str(group.get("id"))] = {
                "impressions": int(values.get("impressions") or 0), "views": int(values.get("views") or 0),
                "contacts": int(values.get("contacts") or 0),
                "contacts_phone": int(values.get("contactsShowPhone") or 0),
                "contacts_chat": int(values.get("contactsMessenger") or 0),
                "favorites": int(values.get("favorites") or 0), "spend": float(spend),
                "bonus": float(_kopecks(values.get("spendingBonus"))),
                "promo_spend": float(_kopecks(values.get("promoSpending"))),
                "presence_spend": float(_kopecks(values.get("presenceSpending")))}
        offset += len(page)
        if not page or offset >= int(payload.get("dataTotalCount") or 0) or offset >= 3000:
            return result
        await asyncio.sleep(61)


async def items_catalog(client: AvitoClient, limit: int = 500) -> dict[str, dict]:
    """Listing titles/links (active and recently closed), max `limit`."""
    catalog: dict[str, dict] = {}
    for status in ("active", "old,removed,blocked,rejected"):
        for page in range(1, 21):
            data = await client.get("/core/v1/items", params={"per_page": 100, "page": page, "status": status})
            rows = data.get("resources") or []
            for item in rows:
                catalog[str(item.get("id"))] = {"title": item.get("title"), "url": item.get("url"),
                                                 "status": item.get("status"), "price": item.get("price"),
                                                 "category": (item.get("category") or {}).get("name"),
                                                 "address": item.get("address")}
            if len(rows) < 100 or len(catalog) >= limit:
                break
        if len(catalog) >= limit:
            break
    return catalog


# --------------------------------------------------------------------------- Авито Реклама

async def ads_account(client: AvitoClient, account_id: str) -> dict:
    data = await client.get(f"/ads/v1/account/{account_id}")
    account = data.get("account") or {}
    if not account:
        raise AvitoError("Авито Реклама не вернула данные кабинета. Проверьте ID аккаунта.")
    return account


async def ads_balance(client: AvitoClient, account_id: str) -> dict | None:
    try:
        return await client.get(f"/ads/v1/account/{account_id}/balance")
    except AvitoError as exc:
        logger.info("avito ads balance unavailable account=%s: %s", account_id, exc)
        return None


async def ads_campaigns(client: AvitoClient, account_id: str) -> list[dict]:
    campaigns = []
    for page in range(1, 101):
        data = await client.post(f"/ads/v1/account/{account_id}/campaigns", {"filter": {}, "limit": 100, "page": page})
        rows = data.get("campaigns") or []
        campaigns.extend(rows)
        if len(rows) < 100 or len(campaigns) >= int(data.get("total") or 0):
            return campaigns
    raise AvitoError("Слишком много кампаний Авито Рекламы для одной синхронизации")


def campaign_meta(item: dict) -> dict:
    return {"id": str(item.get("id")), "name": item.get("name") or "Кампания без названия",
            "status": item.get("status"), "payment_model": item.get("paymentModel"),
            "campaign_type": item.get("campaignType"), "budget": item.get("budget"),
            "start_date": item.get("startDate"), "end_date": item.get("endDate"),
            "created_at": item.get("createdAt"), "updated_at": item.get("updatedAt")}


def _stats_point(point: dict) -> dict:
    spend = (Decimal(str(point.get("spendKopeks"))) / 100 if point.get("spendKopeks") is not None
             else Decimal(str(point.get("spend") or 0)))
    bonus = (Decimal(str(point.get("spendBonusKopeks"))) / 100 if point.get("spendBonusKopeks") is not None
             else Decimal(str(point.get("spendBonus") or 0)))
    return {"spend": spend.quantize(Decimal("0.01")), "bonus": bonus.quantize(Decimal("0.01")),
            "impressions": int(point.get("views") or 0), "clicks": int(point.get("clicks") or 0)}


def _campaign_active_in(item: dict, start: date, end: date) -> bool:
    if item.get("status") in {"draft", "in_moderation", "moderation_failed"}:
        return False
    try:
        first = date.fromisoformat(str(item.get("startDate"))[:10]) if item.get("startDate") else None
        last = date.fromisoformat(str(item.get("endDate"))[:10]) if item.get("endDate") else None
    except ValueError:
        return True
    return not ((last and last < start) or (first and first > end))


async def ads_campaign_stats(client: AvitoClient, account_id: str, campaign_id: str, start: date, end: date) -> dict:
    return await client.post(f"/ads/v1/account/{account_id}/campaigns/{campaign_id}/stats",
                             {"dateFrom": start.isoformat(), "dateTo": end.isoformat()})


async def ads_metrics(client: AvitoClient, row: AdConnection) -> tuple[list[dict], list[dict]]:
    start, end = _sync_window(row, ADS_FIRST_SYNC_DAYS)
    campaigns = await ads_campaigns(client, row.external_account_id)
    result: dict[date, dict] = {}
    for item in campaigns:
        if not _campaign_active_in(item, start, end):
            continue
        meta = campaign_meta(item)
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=ADS_STATS_CHUNK - 1))
            data = await ads_campaign_stats(client, row.external_account_id, meta["id"], chunk_start, chunk_end)
            for point in (data.get("campaign") or {}).get("data") or []:
                stamp = str(point.get("timestamp") or "")[:10]
                if not stamp:
                    continue
                day = date.fromisoformat(stamp)
                values = _stats_point(point)
                daily = result.setdefault(day, {"date": day, "spend": Decimal("0"), "impressions": 0, "clicks": 0,
                                                "leads": 0, "raw": {"bonus": "0"}, "campaigns": []})
                daily["spend"] += values["spend"]
                daily["impressions"] += values["impressions"]
                daily["clicks"] += values["clicks"]
                daily["raw"]["bonus"] = str(Decimal(daily["raw"]["bonus"]) + values["bonus"])
                daily["campaigns"].append({"date": day, "external_campaign_id": meta["id"],
                                           "campaign_name": meta["name"], "spend": values["spend"],
                                           "impressions": values["impressions"], "clicks": values["clicks"],
                                           "raw": {**point, "payment_model": meta["payment_model"],
                                                   "campaign_type": meta["campaign_type"]}})
            chunk_start = chunk_end + timedelta(days=1)
    return [result[day] for day in sorted(result)], [campaign_meta(item) for item in campaigns]


# --------------------------------------------------------------------------- shared entry points

async def test(db, row: AdConnection) -> dict:
    client = await client_for(db, row)
    if row.platform == ITEMS:
        profile = await items_profile(client)
        user_id = str(profile["id"])
        if row.external_account_id not in {"pending", "", user_id}:
            raise AvitoError("Ключи принадлежат другому профилю Авито")
        row.external_account_id = user_id
        balance = await items_balance(client, user_id)
        set_config(row, profile_name=profile.get("name"), profile_url=profile.get("profile_url"),
                   balance=balance, balance_at=_now().isoformat())
        return {"name": profile.get("name"), "currency": "RUB", "remote_status": "active", "balance": balance}
    account = await ads_account(client, row.external_account_id)
    balance = await ads_balance(client, row.external_account_id)
    set_config(row, profile_name=account.get("shortName"), inn=account.get("inn"),
               manager=account.get("manager"), balance=balance, balance_at=_now().isoformat())
    return {"name": account.get("shortName"), "currency": "RUB", "remote_status": "active", "balance": balance}


async def metrics(db, row: AdConnection) -> list[dict]:
    client = await client_for(db, row)
    if row.platform == ITEMS:
        data = await items_metrics(client, row)
        balance = await items_balance(client, row.external_account_id)
        if balance is not None:
            set_config(row, balance=balance, balance_at=_now().isoformat())
        return data
    data, campaigns = await ads_metrics(client, row)
    balance = await ads_balance(client, row.external_account_id)
    set_config(row, campaigns=campaigns[:500], campaigns_at=_now().isoformat(),
               **({"balance": balance, "balance_at": _now().isoformat()} if balance is not None else {}))
    return data
