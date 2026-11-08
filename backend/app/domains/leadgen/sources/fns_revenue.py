"""Доходы ФНС (enricher): выручка за последний год из ГИР БО (bo.nalog.gov.ru — открытый ресурс бухотчётности).

Сверено живым запросом 09.11.2026 (образцы — tests/fixtures/leadgen_sources/fns_*.json):
  GET /advanced-search/organizations/search?query=<ИНН>&page=0 -> {"content": [{"id", "inn": "<strong>…</strong>"…}]}
  GET /nbo/organizations/<id>/bfo/ -> [{"period": "2024", "gainSum": <выручка, тыс. ₽>|null, …}]
ИП бухотчётность не сдают (ИНН из 12 цифр — пропуск). Часть крупных компаний скрыта ФНС — тогда «нет отчётности».
"""
from __future__ import annotations

import re

from app.domains.leadgen import service
from app.domains.leadgen.core.normalize import validate_inn
from app.domains.leadgen.sources.base import CompanySource, Enrichment, SignalIn, SourceFormatError

BASE = "https://bo.nalog.gov.ru"
PAUSE = 0.5


def pick_revenue(reports: list) -> tuple[int, int] | None:
    """(выручка, ₽; год) по самому позднему отчёту, где выручка указана."""
    if not isinstance(reports, list):
        raise SourceFormatError()
    best = None
    for r in reports:
        period = str((r or {}).get("period") or "")
        gain = (r or {}).get("gainSum")
        if not period.isdigit() or gain is None:
            continue
        if best is None or int(period) > best[1]:
            best = (int(round(float(gain) * 1000)), int(period))
    return best


class FnsRevenueSource(CompanySource):
    code = "fns_revenue"
    title = "Доходы ФНС"
    icon = "📊"
    purpose = "Выручка за последний год из бухотчётности ФНС (ГИР БО)"
    requires_site = False
    persist_work = True
    unit_seconds = 2.0       # два запроса + пауза
    concurrency = 2
    settings_schema = [
        {"key": "revenue_min", "label": "Целевая выручка от, ₽", "type": "number", "default": 10_000_000},
        {"key": "revenue_max", "label": "Целевая выручка до, ₽", "type": "number", "default": 500_000_000,
         "hint": "Выше — сигнал «Крупная компания» (для очистки сетей)"},
    ]
    signals_info = [{"kind": "revenue_fit", "label": "Выручка в целевом диапазоне"},
                    {"kind": "big_company", "label": "Крупная компания"}]

    async def enrich(self, company, settings, ctx):
        inn = validate_inn(company.inn)
        if not inn:
            return Enrichment(status="skipped", error="нет ИНН")
        if len(inn) == 12:
            return Enrichment(status="skipped", error="ИП не сдают бухотчётность")
        async with ctx.http(Accept="application/json") as http:
            resp = await http.get(f"{BASE}/advanced-search/organizations/search", params={"query": inn, "page": 0})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or "content" not in data:
                raise SourceFormatError()
            org = next((o for o in data["content"] or []
                        if re.sub(r"<[^>]+>", "", str(o.get("inn") or "")) == inn), None)
            if org is None:
                return Enrichment(status="skipped", error="нет отчётности в ГИР БО")
            await ctx.sleep(PAUSE)
            resp = await http.get(f"{BASE}/nbo/organizations/{int(org['id'])}/bfo/")
            resp.raise_for_status()
            picked = pick_revenue(resp.json())
        if picked is None:
            return Enrichment(status="skipped", error="в отчётности нет выручки")
        revenue, year = picked
        low = int(settings.get("revenue_min") or 0)
        high = int(settings.get("revenue_max") or 0)
        signals = []
        if high and revenue > high:
            signals.append(SignalIn("big_company", {"revenue_rub": revenue, "year": year}, ttl_days=400))
        elif revenue >= low:
            signals.append(SignalIn("revenue_fit", {"revenue_rub": revenue, "year": year}, ttl_days=400))
        return Enrichment(profile={"revenue_rub": revenue, "revenue_year": year, "org_id": org["id"]},
                          signals=signals)

    async def persist(self, db, company, enrichment, ctx):
        """Выручка ФНС свежее и точнее прочих источников: пишем, если год не старше уже записанного.
        Ручную правку не трогаем."""
        if enrichment.status != "done":
            return enrichment
        p = enrichment.profile
        prov = dict(company.provenance or {})
        manual = (prov.get("revenue_rub") or {}).get("source") == "manual"
        if not manual and (not company.revenue_year or p["revenue_year"] >= company.revenue_year):
            company.revenue_rub, company.revenue_year = p["revenue_rub"], p["revenue_year"]
            at = service.utcnow().isoformat()
            prov["revenue_rub"] = prov["revenue_year"] = {"source": "fns", "url": None, "at": at}
            company.provenance = prov
        return enrichment


ADAPTER = FnsRevenueSource()
