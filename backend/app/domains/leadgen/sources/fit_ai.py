"""Оценка ЦА (enricher): обёртка над существующей «Оценить ИИ» (fit_ai.assess) — кнопка панели и шаг цепочки.
Расход ИИ — резерв агентства, как и раньше."""
from __future__ import annotations

from app.domains.leadgen import fit_ai as legacy_fit
from app.domains.leadgen import service
from app.domains.leadgen.sources.base import CompanySource, Enrichment
from app.domains.leadgen.sources.legacy import fresh_signals, step_row


class FitAiSource(CompanySource):
    code = "fit_ai"
    title = "Оценка ЦА"
    icon = "🎯"
    purpose = "ИИ: подходит / возможно / не подходит под портрет клиента"
    requires_site = False
    persist_work = True
    unit_seconds = 3.0
    concurrency = 4   # ИИ — несколько запросов одновременно
    ai_tokens_per_company = 1500
    signals_info = [{"kind": "chain", "label": "Федеральная сеть"}]

    def ready(self, settings):
        from app.domains.leadgen import routes
        return None if routes.fit_available() else "ИИ не подключён в настройках портала"

    async def enrich(self, company, settings, ctx):
        return Enrichment()

    async def persist(self, db, company, enrichment, ctx):
        from app.domains.leadgen import routes
        started = service.utcnow()
        result = await legacy_fit.assess(db, company, now=started, complete=routes.fit_complete)
        if result.get("status") != "done":
            return Enrichment(status="failed", error="ИИ не ответил")
        row = await step_row(db, company.id, self.code)
        return Enrichment(profile=dict(row.result or {}) if row else {},
                          signals=await fresh_signals(db, company.id, self.code, started))


ADAPTER = FitAiSource()
