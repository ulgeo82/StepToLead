"""DaData по ИНН: юрлицо, руководитель, статус, ОКВЭД, выручка. Ключ — DADATA_TOKEN в окружении.

Бесплатно до 10 000 запросов в сутки. Ликвидированные и банкроты получают legal_status=liquidated -> скоринг 0.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.core.normalize import clean_company_name, validate_inn
from app.domains.leadgen.models import LgCompany, LgEnrichment, LgSignal

URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
STEP = "dadata"
STATUS = {"ACTIVE": "active", "REORGANIZING": "active",
          "LIQUIDATING": "liquidated", "LIQUIDATED": "liquidated", "BANKRUPT": "liquidated"}


class DadataNotConfigured(RuntimeError):
    pass


class DadataProvider:
    def __init__(self, token: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0):
        self.token = token or os.getenv("DADATA_TOKEN", "")
        if not self.token:
            raise DadataNotConfigured("DADATA_TOKEN не задан")
        self.transport, self.timeout = transport, timeout

    async def find_party(self, inn: str) -> dict | None:
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            resp = await client.post(URL, json={"query": inn, "branch_type": "MAIN", "count": 1},
                                     headers={"Authorization": f"Token {self.token}", "Accept": "application/json"})
            resp.raise_for_status()
            suggestions = resp.json().get("suggestions") or []
            return suggestions[0].get("data") if suggestions else None


def parse_party(data: dict) -> dict:
    name = data.get("name") or {}
    mgmt = data.get("management") or {}
    state = data.get("state") or {}
    finance = data.get("finance") or {}
    revenue = finance.get("revenue")
    return {
        "legal_name": name.get("short_with_opf") or name.get("full_with_opf"),
        "short_name": name.get("short") or name.get("full"),
        "ogrn": data.get("ogrn"),
        "okved": data.get("okved"),
        "director_name": mgmt.get("name"),
        "director_post": (mgmt.get("post") or "").strip().capitalize() or None,
        "legal_status": STATUS.get(state.get("status") or "", "unknown"),
        "revenue_rub": int(revenue) if isinstance(revenue, (int, float)) else None,
        "revenue_year": finance.get("year"),
        "address": (data.get("address") or {}).get("value"),
    }


async def enrich_legal(db: AsyncSession, company: LgCompany, provider: DadataProvider, *,
                       now: datetime | None = None) -> dict:
    now = now or service.utcnow()
    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id, LgEnrichment.step == STEP))
    if row is None:
        row = LgEnrichment(company_id=company.id, step=STEP)
        db.add(row)
    row.ran_at, row.cost_rub = now, 0
    inn = validate_inn(company.inn)
    if not inn:
        row.status, row.error, row.result = "skipped", "нет ИНН", {}
        await db.flush()
        return {"status": "skipped"}
    try:
        data = await provider.find_party(inn)
    except httpx.HTTPError as exc:
        row.status, row.error, row.result = "failed", f"{type(exc).__name__}: {exc}"[:300], {}
        await db.flush()
        return {"status": "failed"}
    if not data:
        row.status, row.error, row.result = "failed", "ИНН не найден в DaData", {}
        await db.flush()
        return {"status": "not_found"}

    info = parse_party(data)
    values = {k: info[k] for k in ("legal_name", "ogrn", "okved", "director_name", "director_post",
                                   "revenue_rub", "revenue_year", "legal_status")}
    prov = dict(company.provenance or {})
    for key, value in values.items():
        if value not in (None, ""):
            setattr(company, key, value)
            prov[key] = {"source": "dadata", "url": None, "at": now.isoformat()}
    if not company.display_name and info["short_name"]:
        company.display_name = clean_company_name(info["short_name"])
    company.provenance = prov
    if company.legal_status == "active":
        db.add(LgSignal(company_id=company.id, kind="legal_active", key=STEP, payload={"okved": company.okved},
                        observed_at=now, expires_at=now + timedelta(days=180)))
    row.status, row.error, row.result = "done", None, info
    await db.flush()
    await service.recalc_score(db, company, now=now)
    return {"status": "done", "legal_status": company.legal_status}
