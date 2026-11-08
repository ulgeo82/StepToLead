"""DaData (enricher): реквизиты по ИНН — юрлицо, ОКВЭД, директор, статус. Обёртка над dadata.enrich_legal (логика та же).
Ключ — тот же, что у DaData в портале; не настроена — ready_reason."""
from __future__ import annotations

from app.domains.leadgen import dadata as legacy_dadata
from app.domains.leadgen import service
from app.domains.leadgen.sources.base import CompanySource, Enrichment
from app.domains.leadgen.sources.legacy import fresh_signals, step_row


def provider():
    from app.domains.leadgen import routes
    return routes.dadata_factory()


class DadataSource(CompanySource):
    code = "dadata"
    title = "DaData"
    icon = "🏛"
    purpose = "Реквизиты по ИНН: юрлицо, ОКВЭД, директор, статус"
    requires_site = False
    persist_work = True
    unit_seconds = 0.6
    concurrency = 5   # лимит DaData ~30 запросов/с — с запасом
    signals_info = [{"kind": "legal_active", "label": "Действующее юрлицо"}]

    def ready(self, settings):
        try:
            provider()
        except Exception:  # noqa: BLE001 — DadataNotConfigured / HTTPException
            return "DaData не настроена в портале"
        return None

    async def enrich(self, company, settings, ctx):
        return Enrichment() if company.inn else Enrichment(status="skipped", error="нет ИНН")

    async def persist(self, db, company, enrichment, ctx):
        if enrichment.status != "done":
            return enrichment
        started = service.utcnow()
        result = await legacy_dadata.enrich_legal(db, company, provider(), now=started)
        row = await step_row(db, company.id, self.code)
        status = result.get("status")
        if status == "done":
            return Enrichment(profile=dict(row.result or {}) if row else {},
                              signals=await fresh_signals(db, company.id, self.code, started))
        if status == "not_found":
            return Enrichment(status="skipped", error="ИНН не найден в DaData")
        if status == "skipped":
            return Enrichment(status="skipped", error="нет ИНН")
        return Enrichment(status="failed", error="DaData не ответила")


ADAPTER = DadataSource()
