"""Запуск «Поиск компаний» по Яндекс Директу: ключи × регион -> рекламодатели в базе + сигналы.

Сеть только через провайдера (SerpProvider). В тестах — подставной провайдер.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.core.serp import (
    DIRECT_MIN_KEYWORDS, SerpAd, SerpOrganic, aggregate, aggregate_organic, run_fingerprint,
)
from app.domains.leadgen.models import LgAd, LgCompany, LgSignal, LgSourceRun

SOURCE = "yandex_direct"
NEW_ADVERTISER_TTL = timedelta(days=14)
AD_SIGNAL_TTL = timedelta(days=8)       # еженедельный перезапуск продлевает сигнал
ADS_STOPPED_TTL = timedelta(days=30)
ORGANIC_TTL = timedelta(days=30)
ORGANIC_SOURCE = "yandex_organic"
DEVICES = ("desktop", "mobile")


class SerpProvider(Protocol):
    name: str
    cost_per_request_rub: float

    async def search(self, keyword: str, region_code: int | None) -> list[SerpAd]: ...


def _clean_keywords(keywords: list[str]) -> list[str]:
    seen, out = set(), []
    for k in keywords:
        k = " ".join(k.split())
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def clean_devices(devices) -> list[str]:
    out = [d for d in DEVICES if d in (devices or ())]
    return out or ["desktop"]


def estimate(keywords: list[str], provider: SerpProvider, devices=None) -> dict:
    n = len(_clean_keywords(keywords)) * len(clean_devices(devices))
    return {"requests": n, "cost_rub": round(n * provider.cost_per_request_rub, 2)}


async def _page(provider, keyword: str, region_code: int | None, device: str):
    """Страница выдачи: у настоящего провайдера — реклама и органика, у простого — только реклама с ПК."""
    from app.domains.leadgen.providers import SerpPage
    if hasattr(provider, "search_page"):
        return await provider.search_page(keyword, region_code, device)
    if device != "desktop":
        return SerpPage()
    return SerpPage(await provider.search(keyword, region_code))


async def create_run(db: AsyncSession, workspace_id: int, *, keywords: list[str], region_code: int | None,
                     niche: str | None, city: str | None, created_by_id: int | None = None) -> LgSourceRun:
    kws = _clean_keywords(keywords)
    if not kws:
        raise ValueError("no keywords")
    if len(kws) > 500:
        raise ValueError("too many keywords (max 500)")
    run = LgSourceRun(workspace_id=workspace_id, source=SOURCE, status="queued", created_by_id=created_by_id,
                      params={"keywords": kws, "region_code": region_code, "niche": niche, "city": city,
                              "fingerprint": run_fingerprint(SOURCE, region_code, kws)},
                      stats={})
    db.add(run)
    await db.flush()
    return run


async def _previous_domains(db: AsyncSession, run: LgSourceRun) -> set[int] | None:
    """Компании, найденные прошлым завершённым запуском с тем же набором ключей и регионом."""
    prev = (await db.execute(
        select(LgSourceRun).where(LgSourceRun.workspace_id == run.workspace_id, LgSourceRun.source == SOURCE,
                                  LgSourceRun.status == "done", LgSourceRun.id != run.id)
        .order_by(LgSourceRun.id.desc())
    )).scalars().all()
    fp = run.params.get("fingerprint")
    for p in prev:
        if (p.params or {}).get("fingerprint") == fp:
            return set((p.stats or {}).get("company_ids") or [])
    return None


async def execute_run(db: AsyncSession, run: LgSourceRun, provider: SerpProvider, *,
                      now: datetime | None = None) -> LgSourceRun:
    """Выполняет запуск. Ошибка по одному ключу не валит весь запуск: ключ попадает в stats.failed."""
    now = now or service.utcnow()
    params = run.params or {}
    run.status, run.started_at = "running", now
    await db.flush()

    settings = await service.get_settings(db)
    devices = clean_devices(params.get("devices"))
    want_organic = bool(params.get("organic"))
    ads: list[SerpAd] = []
    organic: list[SerpOrganic] = []
    failed: dict[str, str] = {}
    requests = 0
    for kw in params["keywords"]:
        for device in devices:
            requests += 1
            try:
                page = await _page(provider, kw, params.get("region_code"), device)
                ads.extend(page.ads)
                if want_organic and device == "desktop":
                    organic.extend(page.organic)
            except Exception as exc:  # noqa: BLE001 — запуск продолжается, причина в отчёте
                key = kw if device == "desktop" else f"{kw} (телефон)"
                failed[key] = f"{type(exc).__name__}: {exc}"[:300]

    advertisers = aggregate(ads, platforms=tuple(settings["platform_domains"]))
    company_ids: list[int] = []
    new_ids: list[int] = []
    merged = 0
    for adv in sorted(advertisers.values(), key=lambda a: -a.keyword_count):
        res = await service.upsert_company(db, run.workspace_id, service.FindingIn(
            source=SOURCE, domain=adv.domain, city=params.get("city"), region_code=params.get("region_code"),
            niche=params.get("niche"), source_url=adv.landing_url), now=now)
        company = res.company
        if company is None:
            continue
        had_ads = bool(await db.scalar(select(LgAd.id).where(LgAd.company_id == company.id).limit(1)))
        if not res.created:
            merged += 1
        await _store_ads(db, company, adv, params.get("region_code"), now)

        kw_list = sorted(adv.keywords)
        if adv.keyword_count >= DIRECT_MIN_KEYWORDS:
            await _signal(db, company, "ad_direct", run, now, AD_SIGNAL_TTL, {"keywords": kw_list})
        if adv.premium:
            await _signal(db, company, "ad_premium", run, now, AD_SIGNAL_TTL, {})
        if res.created or not had_ads:
            await _signal(db, company, "new_advertiser", run, now, NEW_ADVERTISER_TTL, {"keywords": kw_list})
            new_ids.append(company.id)
        await service.recalc_score(db, company, now=now)
        company_ids.append(company.id)

    stopped = 0
    prev = await _previous_domains(db, run)
    if prev:
        for cid in prev - set(company_ids):
            company = await db.get(LgCompany, cid)
            if company is not None:
                await _signal(db, company, "ads_stopped", run, now, ADS_STOPPED_TTL, {})
                await service.recalc_score(db, company, now=now)
                stopped += 1

    # Органика: компании из топ-10 поиска, которые (пока) не рекламируются по этим ключам.
    organic_ids: list[int] = []
    organic_new: list[int] = []
    if organic:
        hits = aggregate_organic(organic, platforms=tuple(settings["platform_domains"]))
        for hit in sorted(hits.values(), key=lambda h: h.best_position or 99):
            res = await service.upsert_company(db, run.workspace_id, service.FindingIn(
                source=ORGANIC_SOURCE, domain=hit.domain, city=params.get("city"),
                region_code=params.get("region_code"), niche=params.get("niche"), source_url=hit.url), now=now)
            company = res.company
            if company is None:
                continue
            await _signal(db, company, "organic_top", run, now, ORGANIC_TTL,
                          {"keywords": sorted(hit.keywords), "position": hit.best_position, "title": hit.title})
            await service.recalc_score(db, company, now=now)
            if company.id not in company_ids and company.id not in organic_ids:
                organic_ids.append(company.id)
                if res.created:
                    organic_new.append(company.id)

    run.stats = {
        "requests": requests,
        "cost_rub": round(requests * provider.cost_per_request_rub, 2),
        "ads": len(ads),
        "companies": len(company_ids),
        "new_advertisers": len(new_ids),
        "already_in_base": merged,
        "stopped": stopped,
        "failed": failed,
        "company_ids": company_ids,
        "new_company_ids": new_ids,
        "organic_companies": len(organic_ids),
        "organic_new": len(organic_new),
        "organic_company_ids": organic_ids,
        "organic_new_ids": organic_new,
        "devices": devices,
        "provider": provider.name,
    }
    run.status = "failed" if failed and not ads and not organic else "done"
    run.error = "all keywords failed" if run.status == "failed" else None
    run.finished_at = now
    await db.flush()
    return run


async def _store_ads(db: AsyncSession, company: LgCompany, adv, region_code: int | None, now: datetime) -> None:
    existing = {a.content_hash: a for a in (await db.execute(
        select(LgAd).where(LgAd.company_id == company.id))).scalars().all()}
    for view in adv.ads.values():
        row = existing.get(view.content_hash)
        if row is None:
            db.add(LgAd(company_id=company.id, content_hash=view.content_hash, title=view.title, text=view.text,
                        display_url=view.display_url, landing_url=view.landing_url, sitelinks=view.sitelinks,
                        keywords=sorted(view.keywords), region_code=region_code,
                        placement="premium" if view.premium else "other", first_seen_at=now, last_seen_at=now))
        else:
            row.keywords = sorted(set(row.keywords or []) | view.keywords)
            row.last_seen_at = now
            if view.premium:
                row.placement = "premium"
    await db.flush()


async def _signal(db, company, kind, run, now, ttl, payload) -> None:
    db.add(LgSignal(company_id=company.id, run_id=run.id, kind=kind, key=str(run.id), payload=payload,
                    observed_at=now, expires_at=now + ttl))
    await db.flush()
