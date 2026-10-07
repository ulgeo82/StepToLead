"""API «Лидогенерации» для админки агентства: /api/admin/leadgen/*.

Работает в аккаунте агентства (settings.agency_workspace_id). Запись защищена require_admin (включая Origin).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.core.config import settings as app_settings
from app.db import SessionLocal, get_db
from app.domains.leadgen import direct_search, service, site_enrich
from app.domains.leadgen.core.normalize import normalize_domain, normalize_phone
from app.domains.leadgen.models import (
    LgAd, LgCompany, LgContact, LgDnc, LgEnrichment, LgSegment, LgSignal, LgSourceRun, LgTouch,
)
from app.domains.leadgen.providers import ProviderNotConfigured, XmlStockLiveProvider
from app.domains.leadgen.queries import SORTS, FilterError, clean_filters, company_query
from app.models.access import AdminUser
from app.models.system import AppSetting

router = APIRouter(prefix="/admin/leadgen", tags=["admin-leadgen"])

# Подменяются в тестах.
session_factory = SessionLocal
provider_factory = XmlStockLiveProvider
site_transport = None          # httpx-транспорт для тестов
site_delay = site_enrich.DELAY_SECONDS
ENRICH_FRESH_DAYS = 30

DNC_KINDS = ("company", "domain", "email", "phone", "telegram")
DNC_REASONS = ("unsubscribed", "refused", "client", "competitor", "bounced", "legal")


def workspace_id() -> int:
    if not app_settings.agency_workspace_id:
        raise HTTPException(503, "Не задан AGENCY_WORKSPACE_ID — лидогенерация работает в аккаунте агентства")
    return app_settings.agency_workspace_id


def make_provider():
    try:
        return provider_factory()
    except ProviderNotConfigured as exc:
        raise HTTPException(400, f"Источник не настроен: {exc}")


async def company_or_404(db: AsyncSession, ws: int, company_id: int) -> LgCompany:
    row = await db.get(LgCompany, company_id)
    if row is None or row.workspace_id != ws:
        raise HTTPException(404, "Компания не найдена")
    return row


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def company_row(c: LgCompany) -> dict:
    return {"id": c.id, "display_name": c.display_name, "legal_name": c.legal_name, "domain": c.domain,
            "inn": c.inn, "city": c.city, "niche": c.niche, "score": c.score, "score_reasons": c.score_reasons,
            "stage": c.stage, "fit_label": c.fit_label, "needs_review": c.needs_review,
            "crm_deal_id": c.crm_deal_id, "first_seen_at": iso(c.first_seen_at), "last_seen_at": iso(c.last_seen_at)}


def run_row(r: LgSourceRun) -> dict:
    stats = {k: v for k, v in (r.stats or {}).items() if k not in ("company_ids", "new_company_ids")}
    return {"id": r.id, "source": r.source, "status": r.status, "params": r.params, "stats": stats,
            "error": r.error, "created_at": iso(r.created_at), "started_at": iso(r.started_at),
            "finished_at": iso(r.finished_at)}


# ---------------------------------------------------------------- запуски

class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(min_length=1, max_length=500)
    region_code: int | None = Field(default=None, gt=0)
    niche: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    enrich: bool = True  # после поиска сразу собрать контакты с сайтов новых компаний


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(min_length=1, max_length=500)


@router.post("/runs/estimate")
async def estimate(payload: EstimateIn, admin: AdminUser = Depends(require_admin)):
    return direct_search.estimate(payload.keywords, make_provider())


async def _execute(run_id: int) -> None:
    async with session_factory() as db:
        run = await db.get(LgSourceRun, run_id)
        if run is None or run.status != "queued":
            return
        try:
            await direct_search.execute_run(db, run, make_provider())
            await db.commit()
            enrich_ids = list((run.stats or {}).get("new_company_ids") or []) if (run.params or {}).get("enrich") else []
        except Exception as exc:  # noqa: BLE001 — запуск помечается упавшим, причина видна в интерфейсе
            await db.rollback()
            run = await db.get(LgSourceRun, run_id)
            run.status, run.error, run.finished_at = "failed", f"{type(exc).__name__}: {exc}"[:500], service.utcnow()
            await db.commit()
            return
    if enrich_ids:
        await _enrich(enrich_ids, only_stale=True)


@router.post("/runs", status_code=202)
async def create_run(payload: RunIn, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
                     admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    make_provider()  # 400 сразу, если источник не настроен
    try:
        run = await direct_search.create_run(db, ws, keywords=payload.keywords, region_code=payload.region_code,
                                             niche=payload.niche, city=payload.city, created_by_id=admin.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    run.params = {**run.params, "enrich": payload.enrich}
    await db.commit()
    tasks.add_task(_execute, run.id)
    return run_row(run)


@router.get("/runs")
async def list_runs(limit: int = Query(30, ge=1, le=200), db: AsyncSession = Depends(get_db),
                    admin: AdminUser = Depends(require_admin)):
    rows = (await db.execute(select(LgSourceRun).where(LgSourceRun.workspace_id == workspace_id())
                             .order_by(LgSourceRun.id.desc()).limit(limit))).scalars().all()
    return {"items": [run_row(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    run = await db.get(LgSourceRun, run_id)
    if run is None or run.workspace_id != workspace_id():
        raise HTTPException(404, "Запуск не найден")
    ids = (run.stats or {}).get("company_ids") or []
    new = set((run.stats or {}).get("new_company_ids") or [])
    rows = (await db.execute(select(LgCompany).where(LgCompany.id.in_(ids)))).scalars().all() if ids else []
    ads = {}
    if rows:
        for ad in (await db.execute(select(LgAd).where(LgAd.company_id.in_([c.id for c in rows]))
                                    .order_by(LgAd.last_seen_at.desc()))).scalars().all():
            ads.setdefault(ad.company_id, ad)
    companies = []
    for c in sorted(rows, key=lambda c: (-c.score, c.id)):
        ad = ads.get(c.id)
        item = company_row(c)
        item["is_new"] = c.id in new
        item["ad"] = {"title": ad.title, "text": ad.text, "keywords": ad.keywords,
                      "placement": ad.placement} if ad else None
        companies.append(item)
    return {**run_row(run), "companies": companies}


# ---------------------------------------------------------------- база компаний

@router.get("/companies")
async def list_companies(q: str | None = None, niche: str | None = None, city: list[str] = Query(default=[]),
                         min_score: int | None = Query(None, ge=0, le=10), stage: list[str] = Query(default=[]),
                         channel: list[str] = Query(default=[]), signal: list[str] = Query(default=[]),
                         new_days: int | None = Query(None, ge=1, le=365), needs_review: bool | None = None,
                         segment_id: int | None = None, sort: str = Query("score", pattern="^(score|new|seen|name)$"),
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    filters = {"q": q, "niche": niche, "cities": city, "min_score": min_score, "stages": stage,
               "channels": channel, "signals": signal, "new_days": new_days, "needs_review": needs_review}
    filters = {k: v for k, v in filters.items() if v not in (None, [], "")}
    if segment_id:
        seg = await db.get(LgSegment, segment_id)
        if seg is None or seg.workspace_id != ws:
            raise HTTPException(404, "Сегмент не найден")
        filters = {**seg.filters, **filters}
    try:
        base = company_query(ws, filters, service.utcnow())
    except FilterError as exc:
        raise HTTPException(422, str(exc))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.execute(base.order_by(*SORTS[sort]).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [company_row(c) for c in rows]}


@router.get("/companies/{company_id}")
async def get_company(company_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    c = await company_or_404(db, workspace_id(), company_id)
    ads = (await db.execute(select(LgAd).where(LgAd.company_id == c.id).order_by(LgAd.last_seen_at.desc())
                            .limit(20))).scalars().all()
    contacts = (await db.execute(select(LgContact).where(LgContact.company_id == c.id)
                                 .order_by(LgContact.is_primary.desc(), LgContact.id))).scalars().all()
    signals = (await db.execute(select(LgSignal).where(LgSignal.company_id == c.id)
                                .order_by(LgSignal.observed_at.desc()).limit(30))).scalars().all()
    touches = (await db.execute(select(LgTouch).where(LgTouch.company_id == c.id)
                                .order_by(LgTouch.happened_at.desc()).limit(30))).scalars().all()
    return {
        **company_row(c), "ogrn": c.ogrn, "okved": c.okved, "director_name": c.director_name,
        "director_post": c.director_post, "revenue_rub": c.revenue_rub, "revenue_year": c.revenue_year,
        "legal_status": c.legal_status, "fit_reason": c.fit_reason, "provenance": c.provenance,
        "ads": [{"id": a.id, "title": a.title, "text": a.text, "landing_url": a.landing_url,
                 "keywords": a.keywords, "placement": a.placement, "first_seen_at": iso(a.first_seen_at),
                 "last_seen_at": iso(a.last_seen_at)} for a in ads],
        "contacts": [{"id": x.id, "kind": x.kind, "value": x.value, "is_personal": x.is_personal,
                      "person_name": x.person_name, "source": x.source, "source_url": x.source_url,
                      "found_at": iso(x.found_at), "verify_status": x.verify_status, "bounced": x.bounced}
                     for x in contacts],
        "signals": [{"kind": s.kind, "payload": s.payload, "observed_at": iso(s.observed_at),
                     "expires_at": iso(s.expires_at)} for s in signals],
        "touches": [{"channel": t.channel, "direction": t.direction, "status": t.status, "subject": t.subject,
                     "happened_at": iso(t.happened_at)} for t in touches],
    }


class CanContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(pattern=r"^(email|telegram|whatsapp|call)$")
    contact_id: int | None = None
    sequence_id: str | None = Field(default=None, max_length=64)


@router.post("/companies/{company_id}/can-contact")
async def can_contact(company_id: int, payload: CanContactIn, db: AsyncSession = Depends(get_db),
                      admin: AdminUser = Depends(require_admin)):
    await company_or_404(db, workspace_id(), company_id)
    d = await service.check_can_contact(db, company_id, payload.contact_id, payload.channel,
                                        sequence_id=payload.sequence_id)
    return {"ok": d.ok, "reason": d.reason}


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_id: int
    drop_id: int


@router.post("/companies/merge")
async def merge(payload: MergeIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    await company_or_404(db, ws, payload.keep_id)
    await company_or_404(db, ws, payload.drop_id)
    try:
        kept = await service.merge_companies(db, payload.keep_id, payload.drop_id, reason="manual", actor_id=admin.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()
    return company_row(kept)


# ---------------------------------------------------------------- стоп-лист

class DncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(company|domain|email|phone|telegram)$")
    value: str = Field(min_length=1, max_length=300)
    reason: str = Field(pattern=r"^(unsubscribed|refused|client|competitor|bounced|legal)$")
    note: str | None = Field(default=None, max_length=1000)
    until: datetime | None = None


def dnc_norm(kind: str, value: str) -> str:
    v = value.strip()
    if kind == "domain":
        v = normalize_domain(v) or ""
    elif kind == "phone":
        v = normalize_phone(v) or ""
    elif kind in ("email", "telegram"):
        v = v.lower().lstrip("@")
    if not v:
        raise HTTPException(422, "Не удалось распознать значение")
    return v


@router.get("/dnc")
async def list_dnc(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    rows = (await db.execute(select(LgDnc).where(LgDnc.workspace_id == workspace_id())
                             .order_by(LgDnc.id.desc()).limit(1000))).scalars().all()
    return {"items": [{"id": r.id, "kind": r.kind, "value": r.value_norm, "reason": r.reason, "note": r.note,
                       "until": iso(r.until), "created_at": iso(r.created_at)} for r in rows]}


@router.post("/dnc", status_code=201)
async def add_dnc(payload: DncIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    value = dnc_norm(payload.kind, payload.value)
    company = None
    if payload.kind == "company":
        if not value.isdigit():
            raise HTTPException(422, "Для kind=company передайте id компании")
        company = await company_or_404(db, ws, int(value))
    existing = await db.scalar(select(LgDnc).where(LgDnc.workspace_id == ws, LgDnc.kind == payload.kind,
                                                   LgDnc.value_norm == value))
    row = existing or LgDnc(workspace_id=ws, kind=payload.kind, value_norm=value, created_by_id=admin.id)
    row.reason, row.note, row.until = payload.reason, payload.note, payload.until
    db.add(row)
    if company is not None and payload.until is None:
        company.stage = "dnc"
    await db.flush()
    if company is not None:
        await service.recalc_score(db, company)
    await db.commit()
    return {"id": row.id, "kind": row.kind, "value": row.value_norm}


@router.delete("/dnc/{dnc_id}", status_code=204)
async def delete_dnc(dnc_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await db.get(LgDnc, dnc_id)
    if row is None or row.workspace_id != workspace_id():
        raise HTTPException(404, "Запись не найдена")
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------- сегменты

class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=180)
    filters: dict = Field(default_factory=dict)


async def segment_out(db: AsyncSession, ws: int, seg: LgSegment) -> dict:
    count = await db.scalar(select(func.count()).select_from(company_query(ws, seg.filters, service.utcnow()).subquery()))
    return {"id": seg.id, "name": seg.name, "filters": seg.filters, "count": count}


@router.get("/segments")
async def list_segments(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    rows = (await db.execute(select(LgSegment).where(LgSegment.workspace_id == ws).order_by(LgSegment.id))).scalars().all()
    return {"items": [await segment_out(db, ws, s) for s in rows]}


@router.post("/segments", status_code=201)
async def create_segment(payload: SegmentIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    try:
        filters = clean_filters(payload.filters)
    except FilterError as exc:
        raise HTTPException(422, str(exc))
    seg = LgSegment(workspace_id=ws, name=payload.name.strip(), filters=filters, created_by_id=admin.id)
    db.add(seg)
    await db.commit()
    return await segment_out(db, ws, seg)


@router.put("/segments/{segment_id}")
async def update_segment(segment_id: int, payload: SegmentIn, db: AsyncSession = Depends(get_db),
                         admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    seg = await db.get(LgSegment, segment_id)
    if seg is None or seg.workspace_id != ws:
        raise HTTPException(404, "Сегмент не найден")
    try:
        seg.filters = clean_filters(payload.filters)
    except FilterError as exc:
        raise HTTPException(422, str(exc))
    seg.name = payload.name.strip()
    await db.commit()
    return await segment_out(db, ws, seg)


@router.delete("/segments/{segment_id}", status_code=204)
async def delete_segment(segment_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    seg = await db.get(LgSegment, segment_id)
    if seg is None or seg.workspace_id != workspace_id():
        raise HTTPException(404, "Сегмент не найден")
    await db.delete(seg)
    await db.commit()


# ---------------------------------------------------------------- настройки

class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: dict[str, int] | None = None
    platform_domains: list[str] | None = Field(default=None, max_length=500)
    recontact_days: int | None = Field(default=None, ge=1, le=3650)
    outreach_pipeline_id: int | None = None


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    return await service.get_settings(db)


@router.put("/settings")
async def put_settings(payload: SettingsIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    current = await service.get_settings(db)
    patch = payload.model_dump(exclude_none=True)
    if "weights" in patch:
        if any(not -10 <= v <= 10 for v in patch["weights"].values()):
            raise HTTPException(422, "Вес сигнала: от −10 до 10")
        patch["weights"] = {**current["weights"], **patch["weights"]}
    if "platform_domains" in patch:
        patch["platform_domains"] = sorted({d for d in (normalize_domain(x) for x in patch["platform_domains"]) if d})
    value = {**current, **patch}
    row = await db.get(AppSetting, service.SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=service.SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()
    return value


# ---------------------------------------------------------------- обогащение с сайта

class EnrichIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(default_factory=list, max_length=500)
    segment_id: int | None = None
    only_stale: bool = True  # пропускать компании, сайт которых проверяли за последние 30 дней


async def _enrich(company_ids: list[int], *, only_stale: bool) -> None:
    from datetime import timedelta
    for cid in company_ids:
        async with session_factory() as db:
            company = await db.get(LgCompany, cid)
            if company is None or not company.domain:
                continue
            if only_stale:
                done = await db.scalar(select(LgEnrichment.ran_at).where(
                    LgEnrichment.company_id == cid, LgEnrichment.step == site_enrich.STEP,
                    LgEnrichment.status == "done"))
                if done and service._aware(done) > service.utcnow() - timedelta(days=ENRICH_FRESH_DAYS):
                    continue
            try:
                await site_enrich.enrich_company(db, company, transport=site_transport, delay=site_delay)
                await db.commit()
            except Exception:  # noqa: BLE001 — одна компания не должна останавливать очередь
                await db.rollback()


@router.post("/companies/enrich", status_code=202)
async def enrich(payload: EnrichIn, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
                 admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    ids = list(dict.fromkeys(payload.company_ids))
    if payload.segment_id:
        seg = await db.get(LgSegment, payload.segment_id)
        if seg is None or seg.workspace_id != ws:
            raise HTTPException(404, "Сегмент не найден")
        q = company_query(ws, seg.filters, service.utcnow()).with_only_columns(LgCompany.id).limit(500)
        ids += [i for (i,) in (await db.execute(q)).all() if i not in ids]
    if not ids:
        raise HTTPException(422, "Передайте company_ids или segment_id")
    rows = (await db.execute(select(LgCompany.id).where(LgCompany.workspace_id == ws, LgCompany.id.in_(ids),
                                                        LgCompany.domain.is_not(None)))).all()
    valid = [i for (i,) in rows]
    await db.commit()  # закрываем транзакцию чтения: фоновая задача пишет в своей сессии
    tasks.add_task(_enrich, valid, only_stale=payload.only_stale)
    return {"queued": len(valid), "skipped": len(ids) - len(valid)}
