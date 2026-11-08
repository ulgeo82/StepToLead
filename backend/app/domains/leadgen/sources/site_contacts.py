"""Sequential wrapper around the existing website contact collector, not another crawler."""
from sqlalchemy import delete, select

from app.domains.leadgen import site_enrich
from app.domains.leadgen.models import LgContact, LgEnrichment
from .base import CompanySource, Enrichment


class SiteContactsSource(CompanySource):
    code = "site_contacts"
    title = "Собрать контакты с сайта"
    icon = "📬"
    purpose = "Публичные общие контакты с сайта компании"
    chain_default = True
    unit_seconds = 8
    concurrency = 1
    persist_work = True

    async def enrich(self, company, settings, ctx):
        return Enrichment() if company.domain else Enrichment(status="skipped", error="нет сайта")

    async def persist(self, db, company, enrichment, ctx):
        if enrichment.status != "done":
            return enrichment
        before = set((await db.scalars(select(LgContact.id).where(LgContact.company_id == company.id))).all())
        summary = await site_enrich.enrich_company(db, company, transport=ctx.transport,
                                                 delay=ctx.cache.get("crawl_delay", site_enrich.DELAY_SECONDS))
        # The legacy collector marks personal addresses. New sources never retain newly collected personal data.
        await db.execute(delete(LgContact).where(LgContact.company_id == company.id, LgContact.is_personal.is_(True),
                                               LgContact.id.notin_(before)))
        row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id,
                                                        LgEnrichment.step == site_enrich.STEP))
        personal = [email for email in summary.get("emails", [])
                    if site_enrich._looks_personal(email, company.domain)]
        summary["emails"] = [email for email in summary.get("emails", []) if email not in personal]
        # Free page text can contain employee names and personal details beyond detected addresses.
        # Keep the collector's structured corporate facts, not that free-text snapshot.
        summary.pop("text_excerpt", None)
        if row:
            row.result = dict(summary)
        return Enrichment(status=row.status if row else "skipped", profile=summary,
                          error=None if row and row.status == "done" else "сайт недоступен или обход запрещён")


ADAPTER = SiteContactsSource()
