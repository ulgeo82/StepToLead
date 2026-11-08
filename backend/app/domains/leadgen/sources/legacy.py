"""Общее для источников-обёрток над старой логикой (dadata.enrich_legal, fit_ai.assess): та пишет сигналы и
lg_enrichments сама, а раннер при «done» снимает сигналы источника и пишет их заново из Enrichment.signals —
поэтому только что добавленные старой логикой сигналы возвращаем раннеру."""
from __future__ import annotations

from sqlalchemy import select

from app.domains.leadgen.models import LgEnrichment, LgSignal
from app.domains.leadgen.sources.base import SignalIn


async def fresh_signals(db, company_id: int, key: str, since) -> list[SignalIn]:
    await db.flush()
    out = []
    for s in (await db.scalars(select(LgSignal).where(LgSignal.company_id == company_id, LgSignal.key == key,
                                                      LgSignal.observed_at >= since))).all():
        ttl = (s.expires_at - s.observed_at).days if s.expires_at and s.observed_at else None
        out.append(SignalIn(s.kind, dict(s.payload or {}), ttl_days=ttl))
    return out


async def step_row(db, company_id: int, step: str) -> LgEnrichment | None:
    return await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company_id,
                                                      LgEnrichment.step == step))
