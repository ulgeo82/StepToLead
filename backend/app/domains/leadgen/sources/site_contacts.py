"""Обёртка над существующим сбором контактов с сайта (site_enrich), не новый обходчик.

Сеть — в enrich (параллельно по разным сайтам, общий кеш обходов запуска), запись в базу — в persist
(по одной компании, тем же site_enrich.apply_result)."""
from sqlalchemy import select

from app.domains.leadgen import site_enrich
from app.domains.leadgen.models import LgEnrichment
from .base import CompanySource, Enrichment


class SiteContactsSource(CompanySource):
    code = "site_contacts"
    title = "Собрать контакты с сайта"
    icon = "📬"
    purpose = "Публичные общие контакты с сайта компании"
    chain_default = True
    unit_seconds = 5
    concurrency = 12          # разные сайты — параллельно; внутри одного сайта страницы по очереди с паузой
    persist_work = True

    async def enrich(self, company, settings, ctx):
        if not company.domain:
            return Enrichment(status="skipped", error="нет сайта")
        crawl = await site_enrich.crawl(company.domain, transport=ctx.transport, cache=ctx.cache,
                                        delay=ctx.cache.get("crawl_delay", site_enrich.DELAY_SECONDS))
        enr = Enrichment()
        enr.crawl = crawl   # для persist: запись найденного — уже без сети
        return enr

    async def persist(self, db, company, enrichment, ctx):
        if enrichment.status != "done":
            return enrichment
        crawl = getattr(enrichment, "crawl", None)
        summary = await (site_enrich.apply_result(db, company, crawl) if crawl is not None else
                         site_enrich.enrich_company(db, company, transport=ctx.transport, cache=ctx.cache,
                                                    delay=ctx.cache.get("crawl_delay", site_enrich.DELAY_SECONDS)))
        row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id,
                                                        LgEnrichment.step == site_enrich.STEP))
        return Enrichment(status=row.status if row else "skipped", profile=summary,
                          error=None if row and row.status == "done" else "сайт недоступен или обход запрещён")


ADAPTER = SiteContactsSource()
