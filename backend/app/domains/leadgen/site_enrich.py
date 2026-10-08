"""Обогащение с сайта компании: главная + до 5 страниц контактов/реквизитов -> контакты, ИНН, сигналы.

Вежливый обход: robots.txt, пауза между запросами, таймауты, лимит размера. Защиту и капчи не обходим:
не отдался сайт — фиксируем причину и идём дальше.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.core.site_extract import SiteFacts, extract, html_to_text
from app.domains.leadgen.models import LgCompany, LgCompanyKey, LgContact, LgEnrichment, LgSignal

USER_AGENT = "StepToLeadBot/1.0 (+https://steptolead.ru; contact collection from public company pages)"
MAX_PAGES = 6
MAX_BYTES = 1_500_000
TIMEOUT = 10.0
DELAY_SECONDS = 1.0
SIGNAL_TTL_DAYS = 60
STEP = "site_check"


@dataclass
class CrawlResult:
    facts: SiteFacts
    fetched: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    blocked_by_robots: bool = False
    text: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.fetched)


async def _robots(client: httpx.AsyncClient, root: str) -> RobotFileParser | None:
    parser = RobotFileParser()
    try:
        resp = await client.get(f"{root}/robots.txt")
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    parser.parse(resp.text.splitlines())
    return parser


async def _get_html(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None, f"HTTP {resp.status_code}"
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return None, f"not html: {ctype[:40]}"
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            return raw.decode(resp.encoding or "utf-8", errors="replace"), None
    except httpx.HTTPError as exc:
        return None, type(exc).__name__


async def crawl(domain: str, *, transport: httpx.AsyncBaseTransport | None = None,
                delay: float = DELAY_SECONDS) -> CrawlResult:
    result = CrawlResult(facts=SiteFacts())
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, transport=transport,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5",
                                          "Accept-Language": "ru,en;q=0.5"}) as client:
        html, robots, root = None, None, ""
        for scheme in ("https", "http"):
            root = f"{scheme}://{domain}"
            robots = await _robots(client, root)
            if robots is not None and not robots.can_fetch(USER_AGENT, root + "/"):
                result.blocked_by_robots = True
                return result
            html, err = await _get_html(client, root + "/")
            if html is not None:
                break
            result.errors[f"{scheme}:/"] = err or "unreachable"
        if html is None:
            return result
        result.errors.clear()
        result.fetched.append(root + "/")
        first = extract(html, root + "/")
        result.text = html_to_text(html)[:4000]
        result.facts.merge(first)
        queue = [p for p in first.pages if urlsplit(p).hostname]
        seen = {root + "/"}
        while queue and len(result.fetched) < MAX_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                continue
            if delay:
                await asyncio.sleep(delay)
            page, perr = await _get_html(client, url)
            if page is None:
                result.errors[url] = perr or "error"
                continue
            result.fetched.append(url)
            result.facts.merge(extract(page, url))
    return result


async def _signal(db: AsyncSession, company: LgCompany, kind: str, now: datetime, payload: dict) -> None:
    from datetime import timedelta
    db.add(LgSignal(company_id=company.id, kind=kind, key=STEP, payload=payload, observed_at=now,
                    expires_at=now + timedelta(days=SIGNAL_TTL_DAYS)))


async def apply_result(db: AsyncSession, company: LgCompany, result: CrawlResult, *,
                       now: datetime | None = None) -> dict:
    """Пишет найденное в компанию. Возвращает краткую сводку (она же уходит в lg_enrichments.result)."""
    now = now or service.utcnow()
    f = result.facts
    source_url = result.fetched[0] if result.fetched else None
    summary = {
        "pages": result.fetched, "errors": result.errors, "blocked_by_robots": result.blocked_by_robots,
        "phones": sorted(f.phones), "whatsapp": sorted(f.whatsapp), "telegram": sorted(f.telegram),
        "emails": sorted(f.emails), "inns": sorted(f.inns), "has_quiz": f.has_quiz, "crm": sorted(f.crm),
        "chats": sorted(f.chats), "has_metrika": f.has_metrika, "inn_status": None,
        "text_excerpt": result.text[:3000],
    }
    if result.ok:
        existing = set((await db.execute(select(LgContact.kind, LgContact.value_norm)
                                         .where(LgContact.company_id == company.id))).all())
        site_domain = company.domain or ""
        for kind, values in (("phone", f.phones), ("whatsapp", f.whatsapp), ("telegram", f.telegram),
                             ("email", f.emails)):
            for value in sorted(values):
                if (kind, value) in existing:
                    continue
                # Адрес не на домене компании (gmail, mail.ru) — тоже общий канал, но не «именной» по домену.
                personal = kind == "email" and _looks_personal(value, site_domain)
                db.add(LgContact(company_id=company.id, kind=kind, value=value, value_norm=value,
                                 is_personal=personal, source="site", source_url=source_url, found_at=now))
                existing.add((kind, value))
                if kind in ("phone", "whatsapp"):
                    await _add_key(db, company, "phone", value)

        summary["inn_status"] = await _apply_inn(db, company, f.inns, source_url, now)
        if f.ogrns and not company.ogrn and len(f.ogrns) == 1:
            company.ogrn = next(iter(f.ogrns))

        if f.whatsapp or f.telegram:
            await _signal(db, company, "has_messenger", now, {"whatsapp": bool(f.whatsapp), "telegram": bool(f.telegram)})
        if f.has_quiz:
            await _signal(db, company, "site_quiz", now, {})
        if not f.crm:
            await _signal(db, company, "no_crm", now, {"pages_checked": len(result.fetched)})

    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id, LgEnrichment.step == STEP))
    if row is None:
        row = LgEnrichment(company_id=company.id, step=STEP)
        db.add(row)
    row.status = "done" if result.ok else ("skipped" if result.blocked_by_robots else "failed")
    row.result, row.ran_at, row.cost_rub = summary, now, 0
    row.error = None if result.ok else ("robots.txt запрещает обход" if result.blocked_by_robots
                                        else "; ".join(f"{k}: {v}" for k, v in result.errors.items())[:500])
    if result.ok and company.stage == "new":
        company.stage = "enriched"
    await db.flush()
    await service.recalc_score(db, company, now=now)
    return summary


_FREE_MAIL = ("gmail.com", "mail.ru", "yandex.ru", "ya.ru", "bk.ru", "list.ru", "inbox.ru", "rambler.ru", "icloud.com")
_ROLE_BOXES = ("info", "mail", "office", "sales", "zakaz", "order", "hello", "support", "manager", "contact",
               "kontakt", "admin", "shop", "market", "reklama", "pr", "buh", "help", "service", "orders")


def _looks_personal(email: str, domain: str) -> bool:
    """Именной адрес сотрудника на домене компании (ivan.petrov@firma.ru) — персональные данные, по умолчанию не пишем."""
    local, _, host = email.partition("@")
    if host in _FREE_MAIL or not domain or not host.endswith(domain):
        return False
    base = local.split("+")[0].lower()
    return not any(base == r or base.startswith(r) for r in _ROLE_BOXES)


async def _add_key(db: AsyncSession, company: LgCompany, kind: str, value: str) -> None:
    exists = await db.scalar(select(LgCompanyKey.id).where(LgCompanyKey.company_id == company.id,
                                                           LgCompanyKey.kind == kind, LgCompanyKey.value_norm == value))
    if not exists:
        db.add(LgCompanyKey(workspace_id=company.workspace_id, company_id=company.id, kind=kind, value_norm=value))


# ИНН площадок и банков: попадают на сайты из виджетов, оферт и подвалов, к компании отношения не имеют.
PLATFORM_INNS = {
    "7736207543",  # Яндекс
    "7743001840",  # VK
    "7707083893",  # Сбербанк
    "7710140679",  # Т-Банк
    "7704217370",  # Ozon
    "7721546864",  # Wildberries
    "7710668349",  # Авито
    "5405276278",  # 2ГИС
}


async def _apply_inn(db: AsyncSession, company: LgCompany, inns: set[str], url: str | None, now: datetime) -> str | None:
    inns = {i for i in inns if i not in PLATFORM_INNS}
    if not inns:
        return None
    if len(inns) > 1:
        return "several"  # франшиза / несколько юрлиц: не угадываем
    inn = next(iter(inns))
    if company.inn == inn:
        return "same"
    if company.inn and company.inn != inn:
        company.needs_review = company.needs_review or f"inn_mismatch:{inn}"
        return "mismatch"
    other = await db.scalar(select(LgCompany.id).where(LgCompany.workspace_id == company.workspace_id,
                                                       LgCompany.inn == inn, LgCompany.id != company.id))
    if other:
        company.needs_review = f"inn_duplicate:{other}"
        return "duplicate"
    company.inn = inn
    prov = dict(company.provenance or {})
    prov["inn"] = {"source": "site", "url": url, "at": now.isoformat()}
    company.provenance = prov
    await _add_key(db, company, "inn", inn)
    return "set"


async def enrich_company(db: AsyncSession, company: LgCompany, *, transport=None, delay: float = DELAY_SECONDS,
                         now: datetime | None = None) -> dict:
    if not company.domain:
        return {"skipped": "no domain"}
    result = await crawl(company.domain, transport=transport, delay=delay)
    return await apply_result(db, company, result, now=now)
