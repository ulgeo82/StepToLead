"""«Поисковая выдача» (finder): компании из органики Яндекса по запросам ниши через XMLStock (providers.py).

Глубина — 1–5 страниц на запрос. Агрегаторы, каталоги и доски отсекаются списком platform_domains из настроек
лидогенерации (тот же, что в поиске по Директу). Один домен — одна компания.
"""
from __future__ import annotations

import math

from app.domains.leadgen import providers, service
from app.domains.leadgen.core.serp import aggregate_organic
from app.domains.leadgen.core.yandex_regions import lr_for
from app.domains.leadgen.sources.base import Candidate, CompanySource, Context, SignalIn, SourceSkipped, limited, \
    short_error

TOP = 10   # organic_top — в первой десятке хотя бы по одному запросу


def provider_factory():
    """Подменяется в тестах. По умолчанию — тот же провайдер, что у поиска по Директу (routes.provider_factory)."""
    from app.domains.leadgen import routes
    return routes.provider_factory()


class SerpSource(CompanySource):
    code = "serp"
    kind = "finder"
    title = "Поисковая выдача"
    icon = "🔎"
    purpose = "Компании ниши из органической выдачи Яндекса — глубже топ-10"
    unit_seconds = 3.0      # запрос XMLStock с паузой провайдера
    concurrency = 3
    query_schema = [
        {"key": "queries", "type": "list", "label": "Запросы (по одному на строку)", "required": True,
         "ai": {"purpose": "search"}},
        {"key": "region", "type": "region", "label": "Город или регион", "required": True},
        {"key": "depth", "type": "number", "label": "Страниц выдачи на запрос", "default": 2, "min": 1, "max": 5},
        {"key": "niche", "type": "text", "label": "Ниша (метка в базе)"},
    ]
    settings_schema = [{"key": "price_per_1000", "type": "number", "label": "Цена 1000 запросов, ₽", "default": 25}]
    signals_info = [{"kind": "organic_top", "label": "В топ-10 поиска"}]

    @staticmethod
    def depth(query: dict) -> int:
        wanted = max(1, min(5, int(query.get("depth") or 2)))
        return wanted if providers.PAGE_PARAM else 1   # пока параметр страницы не сверен — только первая

    def problem(self, query: dict) -> str | None:
        if query.get("region") and lr_for(query["region"]) is None:
            return f"Не знаю код региона «{query['region']}» — укажите его числом (коды регионов Яндекса, lr)"
        return None

    def units(self, query, filter_settings, settings) -> int:
        return max(1, len(query.get("queries") or [])) * self.depth(query)

    def cost(self, query, settings) -> dict:
        requests = len(query.get("queries") or []) * self.depth(query)
        price = settings.get("price_per_1000")
        rub = round(requests * float(price) / 1000, 2) if price not in (None, "") else None
        return {"requests": requests, "rub": rub}

    async def fetch(self, query: dict, settings: dict, ctx: Context) -> list[Candidate]:
        lr = lr_for(query.get("region"))
        if lr is None:
            raise SourceSkipped(f"не знаю код региона «{query.get('region')}» — укажите его числом (коды Яндекса lr)")
        try:
            provider = provider_factory()
        except Exception as exc:  # noqa: BLE001 — HTTPException 400 из routes или ProviderNotConfigured
            raise SourceSkipped("поиск XMLStock не настроен") from exc
        depth = self.depth(query)
        if depth < int(query.get("depth") or 1):
            ctx.cache.setdefault("errors", {})["serp_depth"] = "взята только первая страница выдачи"
        jobs = [(q, p) for q in query.get("queries") or [] for p in range(depth)]
        failed: list[str] = []

        async def one(job):
            keyword, page = job
            if ctx.on_request:
                ctx.on_request()
            try:
                result = await provider.search_page(keyword, lr, page_no=page) if page else \
                    await provider.search_page(keyword, lr)
            except providers.ProviderUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — один запрос не валит весь поиск
                failed.append(short_error(exc) if not isinstance(exc, providers.ProviderError) else str(exc)[:80])
                return []
            for item in result.organic:            # позиция в общей выдаче, а не на странице
                if item.position:
                    item.position += page * TOP
            return result.organic

        pages = await limited(ctx, jobs, one, self.concurrency)
        if failed and len(failed) == len(jobs):
            raise SourceSkipped(f"поиск не ответил: {failed[0]}")
        if failed:
            ctx.cache.setdefault("errors", {})["serp"] = f"не выполнено запросов: {len(failed)} из {len(jobs)}"
        platforms = tuple((ctx.cache.get("leadgen_settings") or service.DEFAULT_SETTINGS)["platform_domains"])
        hits = aggregate_organic([o for page in pages for o in page], platforms=platforms)
        city = None if str(query.get("region") or "").strip().isdigit() else query.get("region")
        out = []
        for domain, hit in hits.items():
            finding = service.FindingIn(source="yandex_organic", domain=domain, city=city,   # заголовок страницы — не имя
                                        region_code=lr, niche=query.get("niche") or None, source_url=hit.url)
            signals = []
            if hit.best_position and hit.best_position <= TOP:
                signals.append(SignalIn("organic_top", {"position": hit.best_position, "title": hit.title,
                                                        "keywords": sorted(hit.keywords)[:5]}, ttl_days=30))
            out.append(Candidate(finding, signals))
        return out


ADAPTER = SerpSource()
