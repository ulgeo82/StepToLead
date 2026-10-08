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
from app.domains.leadgen import analytics, dadata, direct_search, fit_ai, inbox, outreach, sequences, service, site_enrich
from app.domains.leadgen.core import sequence as seq_core
from app.domains.leadgen.models import LgEnrollment, LgMailbox, LgSequence
from app.core.crypto import encrypt_secret
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
dadata_factory = dadata.DadataProvider
mail_transport_factory = sequences.SmtpImapTransport
fit_complete = None            # подмена ai.complete в тестах
fit_available = fit_ai.available
inbox_complete = None          # подмена ai.complete для разметки входящих в тестах
inbox_available = inbox.available
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
    repeat: bool = False  # повторять каждую неделю (новые и пропавшие рекламодатели)


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
    run.params = {**run.params, "enrich": payload.enrich, "repeat": payload.repeat}
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
                         fit: str | None = Query(None, pattern="^(fit|maybe|no)$"),
                         run_id: int | None = None,  # только компании, найденные этим запуском поиска
                         segment_id: int | None = None, sort: str = Query("score", pattern="^(score|new|seen|name)$"),
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    if await outreach.sync_from_crm(db, ws):
        await db.commit()
    filters = {"q": q, "niche": niche, "cities": city, "min_score": min_score, "stages": stage,
               "channels": channel, "signals": signal, "new_days": new_days, "needs_review": needs_review,
               "fit": fit}
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
    if run_id:
        run = await db.get(LgSourceRun, run_id)
        if run is None or run.workspace_id != ws:
            raise HTTPException(404, "Запуск не найден")
        base = base.where(LgCompany.id.in_([int(i) for i in (run.stats or {}).get("company_ids") or []]))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.execute(base.order_by(*SORTS[sort]).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [company_row(c) for c in rows]}


@router.get("/companies/{company_id}")
async def get_company(company_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    if await outreach.sync_from_crm(db, ws):
        await db.commit()
    c = await company_or_404(db, ws, company_id)
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
    icp: str | None = Field(default=None, min_length=20, max_length=3000)  # портрет клиента для ИИ-оценки


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    value = await service.get_settings(db)
    return {**value, "icp": value.get("icp") or fit_ai.DEFAULT_ICP, "fit_ai_available": fit_available()}


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
    try:
        provider = dadata_factory()
    except dadata.DadataNotConfigured:
        provider = None  # без ключа DaData собираем только сайт
    with_fit = fit_available()
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
                continue
            if provider is not None and company.inn:
                try:
                    await dadata.enrich_legal(db, company, provider)
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
            if with_fit and not company.fit_label:
                try:
                    await fit_ai.assess(db, company, complete=fit_complete)
                    await db.commit()
                except Exception:  # noqa: BLE001
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


# ---------------------------------------------------------------- в аутрич (CRM)

REASONS = {
    "already_in_crm": "уже в CRM", "no_contacts": "нет контактов", "recently_contacted": "писали недавно",
    "crm:client": "уже клиент", "crm:open_deal": "открыта сделка", "sequence:another_active": "идёт другая цепочка",
    "dnc:company": "в стоп-листе", "dnc:domain": "домен в стоп-листе", "stage:rejected": "отказ", "stage:dnc": "в стоп-листе",
}


class OutreachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(default_factory=list, max_length=500)
    segment_id: int | None = None


@router.post("/companies/outreach")
async def to_outreach(payload: OutreachIn, db: AsyncSession = Depends(get_db),
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
    await outreach.sync_from_crm(db, ws)
    results = []
    for cid in ids:
        company = await db.get(LgCompany, cid)
        if company is None or company.workspace_id != ws:
            results.append({"company_id": cid, "ok": False, "reason": "not_found", "reason_text": "не найдена"})
            continue
        r = await outreach.hand_off(db, company)
        results.append({"company_id": cid, "ok": r.ok, "deal_id": r.deal_id, "reason": r.reason,
                        "reason_text": REASONS.get(r.reason or "", r.reason)})
    await db.commit()
    _, pipeline = await outreach.ensure_pipeline(db, ws)
    await db.commit()
    return {"moved": sum(r["ok"] for r in results), "skipped": sum(not r["ok"] for r in results),
            "pipeline_id": pipeline.id, "project_id": pipeline.project_id, "results": results}


@router.get("/outreach/pipeline")
async def outreach_pipeline(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    project, pipeline = await outreach.ensure_pipeline(db, workspace_id())
    await db.commit()
    return {"project_id": project.id, "pipeline_id": pipeline.id, "crm_url": f"/crm?project_id={project.id}"}


class RunPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeat: bool


@router.patch("/runs/{run_id}")
async def patch_run(run_id: int, payload: RunPatch, db: AsyncSession = Depends(get_db),
                    admin: AdminUser = Depends(require_admin)):
    run = await db.get(LgSourceRun, run_id)
    if run is None or run.workspace_id != workspace_id():
        raise HTTPException(404, "Запуск не найден")
    run.params = {**(run.params or {}), "repeat": payload.repeat}
    await db.commit()
    return run_row(run)


# ---------------------------------------------------------------- почтовые ящики

class MailboxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    sender_name: str | None = Field(default=None, max_length=120)
    smtp_host: str = Field(min_length=3, max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    imap_host: str = Field(min_length=3, max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    login: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=500)
    daily_limit: int = Field(default=30, ge=1, le=200)
    warmup: bool = True  # новый ящик: 5 → 10 → 20 писем в день первые три недели


class MailboxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender_name: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, min_length=1, max_length=500)
    daily_limit: int | None = Field(default=None, ge=1, le=200)
    is_active: bool | None = None


async def mailbox_row(db: AsyncSession, mb: LgMailbox) -> dict:
    now = service.utcnow()
    sent = await sequences._sent_today(db, mb.id, now, "Europe/Moscow")
    left = seq_core.mailbox_capacity(mb.daily_limit, mb.warmup_started_on, now.date(), sent)
    return {"id": mb.id, "email": mb.email, "sender_name": mb.sender_name, "smtp_host": mb.smtp_host,
            "smtp_port": mb.smtp_port, "imap_host": mb.imap_host, "imap_port": mb.imap_port, "login": mb.login,
            "daily_limit": mb.daily_limit, "warmup_started_on": mb.warmup_started_on.isoformat() if mb.warmup_started_on else None,
            "is_active": mb.is_active, "sent_today": sent, "left_today": left, "last_error": mb.last_error,
            "last_checked_at": iso(mb.last_checked_at)}


async def mailbox_or_404(db: AsyncSession, mailbox_id: int) -> LgMailbox:
    mb = await db.get(LgMailbox, mailbox_id)
    if mb is None or mb.workspace_id != workspace_id():
        raise HTTPException(404, "Ящик не найден")
    return mb


@router.get("/mailboxes")
async def list_mailboxes(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    rows = (await db.execute(select(LgMailbox).where(LgMailbox.workspace_id == workspace_id())
                             .order_by(LgMailbox.id))).scalars().all()
    return {"items": [await mailbox_row(db, mb) for mb in rows]}


@router.post("/mailboxes", status_code=201)
async def create_mailbox(payload: MailboxIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    email_ = payload.email.strip().lower()
    if await db.scalar(select(LgMailbox.id).where(LgMailbox.workspace_id == ws, LgMailbox.email == email_)):
        raise HTTPException(409, "Такой ящик уже добавлен")
    if email_.endswith("@steptolead.ru"):
        raise HTTPException(422, "Не используйте основной домен steptolead.ru для холодных писем — заведите отдельный")
    mb = LgMailbox(workspace_id=ws, email=email_, sender_name=payload.sender_name, smtp_host=payload.smtp_host.strip(),
                   smtp_port=payload.smtp_port, imap_host=payload.imap_host.strip(), imap_port=payload.imap_port,
                   login=(payload.login or email_).strip(), password_encrypted=encrypt_secret(payload.password),
                   daily_limit=payload.daily_limit, warmup_started_on=service.utcnow().date() if payload.warmup else None,
                   is_active=True)
    db.add(mb)
    await db.commit()
    return await mailbox_row(db, mb)


@router.patch("/mailboxes/{mailbox_id}")
async def patch_mailbox(mailbox_id: int, payload: MailboxPatch, db: AsyncSession = Depends(get_db),
                        admin: AdminUser = Depends(require_admin)):
    mb = await mailbox_or_404(db, mailbox_id)
    data = payload.model_dump(exclude_none=True)
    if "password" in data:
        mb.password_encrypted = encrypt_secret(data.pop("password"))
        mb.last_error = None
    for key, value in data.items():
        setattr(mb, key, value)
    await db.commit()
    return await mailbox_row(db, mb)


@router.post("/mailboxes/{mailbox_id}/check")
async def check_mailbox(mailbox_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    from app.core.crypto import decrypt_secret
    mb = await mailbox_or_404(db, mailbox_id)
    try:
        await mail_transport_factory().check(mb, decrypt_secret(mb.password_encrypted))
    except Exception as exc:  # noqa: BLE001 — показываем причину как есть
        mb.last_error = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        return {"ok": False, "error": mb.last_error}
    mb.last_error = None
    await db.commit()
    return {"ok": True, "error": None}


@router.delete("/mailboxes/{mailbox_id}", status_code=204)
async def delete_mailbox(mailbox_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    mb = await mailbox_or_404(db, mailbox_id)
    busy = await db.scalar(select(func.count(LgEnrollment.id)).where(LgEnrollment.mailbox_id == mb.id,
                                                                     LgEnrollment.status.in_(("active", "paused"))))
    if busy:
        raise HTTPException(409, f"С ящика идут цепочки ({busy}). Сначала отключите его — новые письма не уйдут")
    await db.delete(mb)
    await db.commit()


# ---------------------------------------------------------------- цепочки

class SequenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=180)
    steps: list[dict] = Field(min_length=1, max_length=seq_core.MAX_STEPS)
    window: dict = Field(default_factory=dict)
    is_active: bool = True


def validate_sequence(payload: SequenceIn) -> tuple[list, dict]:
    try:
        steps = seq_core.parse_steps(payload.steps)
        window = seq_core.parse_window(payload.window)
    except seq_core.SequenceError as exc:
        raise HTTPException(422, str(exc))
    return ([{"channel": s.channel, "delay_days": s.delay_days, "subject": s.subject, "body": s.body,
              "new_thread": s.new_thread} for s in steps],
            {"tz": window.tz, "start_hour": window.start_hour, "end_hour": window.end_hour, "weekdays": list(window.weekdays)})


async def sequence_row(db: AsyncSession, seq: LgSequence) -> dict:
    counts = dict((await db.execute(select(LgEnrollment.status, func.count(LgEnrollment.id))
                                    .where(LgEnrollment.sequence_id == seq.id).group_by(LgEnrollment.status))).all())
    return {"id": seq.id, "name": seq.name, "steps": seq.steps, "window": seq.window, "is_active": seq.is_active,
            "counts": counts, "created_at": iso(seq.created_at)}


async def sequence_or_404(db: AsyncSession, sequence_id: int) -> LgSequence:
    seq = await db.get(LgSequence, sequence_id)
    if seq is None or seq.workspace_id != workspace_id():
        raise HTTPException(404, "Цепочка не найдена")
    return seq


@router.get("/sequences")
async def list_sequences(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    rows = (await db.execute(select(LgSequence).where(LgSequence.workspace_id == workspace_id())
                             .order_by(LgSequence.id.desc()))).scalars().all()
    return {"items": [await sequence_row(db, s) for s in rows], "variables": list(seq_core.VARIABLES)}


@router.post("/sequences", status_code=201)
async def create_sequence(payload: SequenceIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    steps, window = validate_sequence(payload)
    seq = LgSequence(workspace_id=workspace_id(), name=payload.name.strip(), steps=steps, window=window,
                     is_active=payload.is_active, created_by_id=admin.id)
    db.add(seq)
    await db.commit()
    return await sequence_row(db, seq)


@router.put("/sequences/{sequence_id}")
async def update_sequence(sequence_id: int, payload: SequenceIn, db: AsyncSession = Depends(get_db),
                          admin: AdminUser = Depends(require_admin)):
    seq = await sequence_or_404(db, sequence_id)
    steps, window = validate_sequence(payload)
    started = await db.scalar(select(func.max(LgEnrollment.current_step)).where(
        LgEnrollment.sequence_id == seq.id, LgEnrollment.status.in_(("active", "paused"))))
    if started and len(steps) < started:
        raise HTTPException(409, "Нельзя удалить шаги, которые уже прошли участники цепочки")
    seq.name, seq.steps, seq.window, seq.is_active = payload.name.strip(), steps, window, payload.is_active
    await db.commit()
    return await sequence_row(db, seq)


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: int
    steps: list[dict] = Field(min_length=1, max_length=seq_core.MAX_STEPS)


@router.post("/sequences/preview")
async def preview_sequence(payload: PreviewIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    company = await company_or_404(db, workspace_id(), payload.company_id)
    try:
        steps = seq_core.parse_steps(payload.steps)
    except seq_core.SequenceError as exc:
        raise HTTPException(422, str(exc))
    mailbox = await db.scalar(select(LgMailbox).where(LgMailbox.workspace_id == company.workspace_id).limit(1))
    ctx = await sequences._context(db, company, mailbox)
    return {"context": ctx, "steps": [{"channel": s.channel, "subject": seq_core.render(s.subject or "", ctx) or None,
                                       "body": seq_core.render(s.body, ctx)} for s in steps]}


class EnrollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(default_factory=list, max_length=500)
    segment_id: int | None = None


ENROLL_REASONS = {**REASONS, "already_in_sequence": "уже в цепочке", "no_email": "нет рабочей почты",
                  "sequence_inactive": "цепочка выключена"}


@router.post("/sequences/{sequence_id}/enroll")
async def enroll(sequence_id: int, payload: EnrollIn, db: AsyncSession = Depends(get_db),
                 admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    seq = await sequence_or_404(db, sequence_id)
    ids = list(dict.fromkeys(payload.company_ids))
    if payload.segment_id:
        segment = await db.get(LgSegment, payload.segment_id)
        if segment is None or segment.workspace_id != ws:
            raise HTTPException(404, "Сегмент не найден")
        q = company_query(ws, segment.filters, service.utcnow()).with_only_columns(LgCompany.id).limit(500)
        ids += [i for (i,) in (await db.execute(q)).all() if i not in ids]
    if not ids:
        raise HTTPException(422, "Передайте company_ids или segment_id")
    if not await db.scalar(select(LgMailbox.id).where(LgMailbox.workspace_id == ws, LgMailbox.is_active.is_(True))) \
            and any(s.get("channel") == "email" for s in seq.steps):
        raise HTTPException(409, "Добавьте и проверьте хотя бы один почтовый ящик")
    results = []
    for cid in ids:
        company = await db.get(LgCompany, cid)
        if company is None or company.workspace_id != ws:
            results.append({"company_id": cid, "ok": False, "reason": "not_found", "reason_text": "не найдена"})
            continue
        r = await sequences.enroll(db, seq, company)
        results.append({"company_id": cid, "ok": r.ok, "enrollment_id": r.enrollment_id, "reason": r.reason,
                        "reason_text": ENROLL_REASONS.get(r.reason or "", r.reason)})
    await db.commit()
    return {"enrolled": sum(r["ok"] for r in results), "skipped": sum(not r["ok"] for r in results), "results": results}


@router.get("/sequences/{sequence_id}/enrollments")
async def list_enrollments(sequence_id: int, status: str | None = None, limit: int = Query(100, ge=1, le=500),
                           db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    seq = await sequence_or_404(db, sequence_id)
    q = select(LgEnrollment, LgCompany).join(LgCompany, LgCompany.id == LgEnrollment.company_id).where(
        LgEnrollment.sequence_id == seq.id)
    if status:
        q = q.where(LgEnrollment.status == status)
    rows = (await db.execute(q.order_by(LgEnrollment.id.desc()).limit(limit))).all()
    return {"items": [{"id": e.id, "company_id": c.id, "company": c.display_name or c.domain, "status": e.status,
                       "current_step": e.current_step, "steps_total": len(seq.steps), "next_at": iso(e.next_at),
                       "stop_reason": e.stop_reason, "started_at": iso(e.started_at)} for e, c in rows]}


class EnrollmentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(pause|resume|stop)$")


@router.post("/enrollments/{enrollment_id}")
async def enrollment_action(enrollment_id: int, payload: EnrollmentAction, db: AsyncSession = Depends(get_db),
                            admin: AdminUser = Depends(require_admin)):
    enr = await db.get(LgEnrollment, enrollment_id)
    if enr is None or enr.workspace_id != workspace_id():
        raise HTTPException(404, "Не найдено")
    now = service.utcnow()
    if payload.action == "pause" and enr.status == "active":
        enr.status = "paused"
    elif payload.action == "resume" and enr.status == "paused":
        seq = await db.get(LgSequence, enr.sequence_id)
        enr.status = "active"
        enr.next_at = max(service._aware(enr.next_at) or now, seq_core.in_window(now, seq_core.parse_window(seq.window)))
    elif payload.action == "stop" and enr.status in ("active", "paused"):
        sequences._finish(enr, "stopped", "manual", now)
    else:
        raise HTTPException(409, f"Нельзя {payload.action} в статусе {enr.status}")
    await db.commit()
    return {"id": enr.id, "status": enr.status, "next_at": iso(enr.next_at)}


# ---------------------------------------------------------------- ИИ-оценка и аналитика

class AssessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(default_factory=list, max_length=200)
    segment_id: int | None = None
    force: bool = False  # переоценить, даже если оценка уже есть


async def _assess(company_ids: list[int], force: bool) -> None:
    for cid in company_ids:
        async with session_factory() as db:
            company = await db.get(LgCompany, cid)
            if company is None or (company.fit_label and not force):
                continue
            try:
                await fit_ai.assess(db, company, complete=fit_complete)
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()


@router.post("/companies/assess", status_code=202)
async def assess(payload: AssessIn, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
                 admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    if not fit_available():
        raise HTTPException(400, "ИИ не подключён в портале (LLM_PROVIDER, модель, ключ) — оценка недоступна")
    ids = list(dict.fromkeys(payload.company_ids))
    if payload.segment_id:
        segment = await db.get(LgSegment, payload.segment_id)
        if segment is None or segment.workspace_id != ws:
            raise HTTPException(404, "Сегмент не найден")
        q = company_query(ws, segment.filters, service.utcnow()).with_only_columns(LgCompany.id).limit(200)
        ids += [i for (i,) in (await db.execute(q)).all() if i not in ids]
    if not ids:
        raise HTTPException(422, "Передайте company_ids или segment_id")
    valid = [i for (i,) in (await db.execute(select(LgCompany.id).where(
        LgCompany.workspace_id == ws, LgCompany.id.in_(ids)))).all()]
    await db.commit()
    tasks.add_task(_assess, valid, payload.force)
    return {"queued": len(valid), "skipped": len(ids) - len(valid)}


@router.get("/analytics")
async def outreach_analytics(days: int = Query(30, ge=1, le=365), niche: str | None = None,
                             db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    if await outreach.sync_from_crm(db, ws):
        await db.commit()
    return await analytics.overview(db, ws, days=days, niche=niche)


# ---------------------------------------------------------------- входящие

@router.get("/inbox")
async def inbox_list(status: str = Query("unhandled", pattern="^(unhandled|handled|all)$"),
                     label: str | None = Query(None, pattern="^(" + "|".join(inbox.LABELS) + ")$"),
                     mailbox_id: int | None = None, q: str | None = Query(None, max_length=200),
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    data = await inbox.listing(db, workspace_id(), status=status, label=label, mailbox_id=mailbox_id, q=q,
                               limit=limit, offset=offset)
    return {**data, "ai_available": inbox_available()}


async def inbox_touch_or_404(db: AsyncSession, touch_id: int):
    found = await inbox.touch_or_none(db, workspace_id(), touch_id)
    if found is None:
        raise HTTPException(404, "Ответ не найден")
    return found


@router.get("/inbox/{touch_id}")
async def inbox_item(touch_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    touch, company = await inbox_touch_or_404(db, touch_id)
    return await inbox.row_out(db, touch, company, full=True)


class InboxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, pattern="^(" + "|".join(inbox.LABELS) + ")$")
    handled: bool | None = None
    remind_days: int | None = Field(default=None, ge=1, le=365)  # для «Позже»: через сколько дней напомнить


@router.patch("/inbox/{touch_id}")
async def inbox_update(touch_id: int, payload: InboxPatch, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    touch, company = await inbox_touch_or_404(db, touch_id)
    applied = inbox.Applied()
    if payload.label:
        applied = await inbox.apply_label(db, touch, company, payload.label, remind_days=payload.remind_days)
    if payload.handled is not None:
        touch.handled_at = (touch.handled_at or service.utcnow()) if payload.handled else None
    await db.commit()
    return {**await inbox.row_out(db, touch, company), "task_id": applied.task_id, "effect": applied.effect}


@router.post("/inbox/{touch_id}/classify")
async def inbox_classify(touch_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    if not inbox_available():
        raise HTTPException(400, "ИИ не подключён в портале — разметка доступна только правилами")
    touch, company = await inbox_touch_or_404(db, touch_id)
    if touch.label_source == "user":
        raise HTTPException(409, "Метку уже поставил человек — ИИ её не меняет")
    touch.label_source = "rule"
    result = await inbox.classify(db, touch, complete=inbox_complete)
    await db.commit()
    if result["status"] != "done":
        raise HTTPException(502, result.get("error") or "ИИ не ответил")
    return await inbox.row_out(db, touch, company)


class InboxReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)


@router.post("/inbox/{touch_id}/reply")
async def inbox_reply(touch_id: int, payload: InboxReply, db: AsyncSession = Depends(get_db),
                      admin: AdminUser = Depends(require_admin)):
    touch, company = await inbox_touch_or_404(db, touch_id)
    try:
        out = await inbox.send_reply(db, mail_transport_factory(), touch, company, payload.body.strip(),
                                     user_id=admin.id)
    except inbox.ReplyError as exc:
        await db.commit()  # сохранить last_error ящика
        raise HTTPException(exc.status, str(exc))
    await db.commit()
    return {"ok": True, "touch_id": out.id, "item": await inbox.row_out(db, touch, company, full=True)}
