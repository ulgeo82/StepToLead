"""Режим «лиды»: компания, которая ищет маркетолога / директолога / РОПа, -> «База компаний» лидогенерации.

Склейка с уже известными компаниями — штатная (upsert_company: hh_employer_id, ИНН, домен, название+город).
Стоп-лист соблюдается. Сделки в CRM не создаются — как и во всей лидогенерации.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar.sources import REGISTRY, Context, Vacancy
from app.domains.leadgen import service
from app.domains.leadgen.core.contact_policy import BLOCKED_STAGES
from app.domains.leadgen.core.normalize import normalize_domain
from app.domains.leadgen.models import LgCompany, LgList, LgListItem, LgSignal

logger = logging.getLogger("uvicorn.error.jobradar")
LIST_NAME = "Нанимает маркетолога"
LIST_KIND = "jobradar"
SIGNAL = "hh_marketing"     # «Ищут маркетолога» — сигнал уже есть в скоринге лидогенерации
SOURCE = "jobradar"


async def hiring_list(db: AsyncSession, workspace_id: int) -> LgList:
    lst = await db.scalar(select(LgList).where(LgList.workspace_id == workspace_id, LgList.kind == LIST_KIND,
                                               LgList.archived_at.is_(None)).limit(1))
    if lst is None:
        lst = LgList(workspace_id=workspace_id, name=LIST_NAME, kind=LIST_KIND,
                     note="Радар вакансий: компании, которые ищут маркетолога, директолога или РОПа")
        db.add(lst)
        await db.flush()
    return lst


async def _domain_blocked(db: AsyncSession, workspace_id: int, domain: str | None, now: datetime) -> bool:
    if not domain:
        return False
    entries = await service._active_dnc(db, workspace_id, now)
    return any(e.kind == "domain" and (domain == e.value_norm or domain.endswith("." + e.value_norm)) for e in entries)


async def _company_blocked(db: AsyncSession, company: LgCompany, now: datetime) -> bool:
    if company.stage in BLOCKED_STAGES:
        return True
    entries = await service._active_dnc(db, company.workspace_id, now)
    return any(e.kind == "company" and e.value_norm == str(company.id) for e in entries)


async def _legal(db: AsyncSession, company: LgCompany, now: datetime) -> None:
    from app.domains.leadgen import dadata, routes as leadgen_routes
    try:
        provider = leadgen_routes.dadata_factory()
    except Exception:  # noqa: BLE001 — DaData не подключена: компания останется с ИНН, без юрлица
        return
    try:
        await dadata.enrich_legal(db, company, provider, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.info("jobradar dadata failed type=%s", type(exc).__name__)


async def add_company(db: AsyncSession, workspace_id: int, cards: list[Vacancy], ctx: Context, hh_settings: dict, *,
                      now: datetime) -> tuple[int | None, str]:
    """(id компании, что сделали) — что сделали: added / known / blocked / skipped."""
    main = next((c for c in cards if c.employer.get("id")), cards[0])
    if not (main.company or "").strip():
        return None, "skipped"
    employer = dict(main.employer)
    site = employer.get("site") or next((c.employer.get("site") for c in cards if c.employer.get("site")), None)
    if not site and employer.get("id") and main.source == "hh" and "hh" in REGISTRY:
        try:
            site = await REGISTRY["hh"].employer_site(employer["id"], hh_settings, ctx)
        except Exception as exc:  # noqa: BLE001 — без сайта компания всё равно попадёт в базу
            logger.info("jobradar hh employer site failed type=%s", type(exc).__name__)
    domain = normalize_domain(site) if site else None
    if await _domain_blocked(db, workspace_id, domain, now):
        return None, "blocked"
    inn = next((c.company_inn or c.employer.get("inn") for c in cards if c.company_inn or c.employer.get("inn")), None)
    finding = service.FindingIn(source=SOURCE, name=main.company, inn=inn, domain=domain,
                                city=None if main.remote else (main.city or None),
                                hh_employer_id=employer.get("id"), source_url=employer.get("url") or main.url)
    result = await service.upsert_company(db, workspace_id, finding, now=now)
    company = result.company
    if company is None:
        return None, "skipped"
    if await _company_blocked(db, company, now):
        return company.id, "blocked"
    for card in cards:
        exists = await db.scalar(select(LgSignal.id).where(LgSignal.company_id == company.id, LgSignal.kind == SIGNAL,
                                                           LgSignal.key == card.uid).limit(1))
        if not exists:
            await service.add_signal(db, company, SIGNAL, key=card.uid, now=now, recalc=False,
                                     payload={"title": card.title, "url": card.url, "source": card.source,
                                              "employer_url": card.employer.get("url")})
    if result.created and company.inn and not company.legal_name:
        await _legal(db, company, now)   # юрлицо по ИНН — через DaData, как при поиске компаний
    await service.recalc_score(db, company, now=now)
    lst = await hiring_list(db, workspace_id)
    if not await db.get(LgListItem, (lst.id, company.id)):
        db.add(LgListItem(list_id=lst.id, company_id=company.id, is_new=True))
    await db.flush()
    return company.id, "added" if result.created else "known"
