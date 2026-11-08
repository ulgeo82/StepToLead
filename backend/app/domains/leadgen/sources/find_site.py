"""Identity-safe site discovery. A search result alone is never evidence of ownership."""
import re
import ipaddress

from app.domains.leadgen import dadata, service, site_enrich
from app.domains.leadgen.core.names import clean_company_name
from app.domains.leadgen.core.normalize import is_platform_domain, normalize_domain, text_key, validate_inn
from app.domains.leadgen.sources.hiring import JOB_BOARDS
from app.domains.leadgen.sources.serp import provider_factory
from .base import CompanySource, Enrichment

DIRECTORIES = ("rusprofile.ru", "list-org.com", "checko.ru", "zachestnyibiznes.ru", "sbis.ru", "audit-it.ru",
               "spark-interfax.ru", "egrul.nalog.ru", "vbankcenter.ru", "companies.rbc.ru")
SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


def location_key(value):
    return re.sub(r"\b(?:город|г|область|обл|республика|респ)\b\.?", "", text_key(value) or "").strip()


def name_key(value):
    return re.sub(r"[^\w]+", "", clean_company_name(value or "")[0].casefold().replace("ё", "е"))


def public_host(domain):
    if not domain or "." not in domain or domain.endswith((".local", ".localhost", ".internal")):
        return False
    try:
        ipaddress.ip_address(domain)
        return False
    except ValueError:
        return True


def unique_party(items, name, city):
    if not city or len(items) >= 20:
        return None
    matches = set()
    for item in items:
        data = item.get("data") or {}
        names = data.get("name") or {}
        address = ((data.get("address") or {}).get("data") or {})
        places = [address.get(k) for k in ("city", "city_with_type", "settlement", "region", "region_with_type")]
        if (data.get("state") or {}).get("status") != "ACTIVE":
            continue
        if not any(location_key(p) == location_key(city) for p in places if p):
            continue
        if not any(name_key(n) == name_key(name) for n in (names.get("short"), names.get("full"),
                                                          names.get("short_with_opf")) if n):
            continue
        inn = validate_inn(data.get("inn"))
        if inn:
            matches.add(inn)
    return next(iter(matches)) if len(matches) == 1 else None


async def checked_site(domain, ctx):
    cache = ctx.cache.setdefault("find_site_crawls", {})
    if domain not in cache:
        cache[domain] = await site_enrich.crawl(domain, transport=ctx.transport,
                                              delay=ctx.cache.get("crawl_delay", site_enrich.DELAY_SECONDS))
    return cache[domain]


class FindSiteSource(CompanySource):
    code = "find_site"
    title = "Найти сайт и ИНН"
    icon = "🌐"
    purpose = "Сайт только с подтверждённым ИНН; ИНН по единственному совпадению в городе"
    requires_site = False
    chain_default = True
    field_source = "site_search"
    unit_seconds = 12
    concurrency = 2
    settings_schema = [{"key": "price_per_1000", "label": "Цена 1000 запросов, ₽", "type": "number", "default": 25}]

    def cost(self, query, settings):
        requests = 2 * int(query.get("companies") or 0)
        price = settings.get("price_per_1000")
        return {"requests": requests, "rub": round(requests * float(price) / 1000, 2) if price is not None else None}

    async def enrich(self, company, settings, ctx):
        inn = validate_inn(company.inn)
        if company.domain:
            if not public_host(company.domain):
                return Enrichment(status="skipped", error="нужен публичный сайт компании")
            if company.inn:
                return Enrichment(status="skipped", error="сайт и ИНН уже есть")
            result = await checked_site(company.domain, ctx)
            inns = {i for i in result.facts.inns if validate_inn(i) and i not in site_enrich.PLATFORM_INNS}
            if not result.ok or len(inns) != 1:
                return Enrichment(status="skipped", error="ИНН сайта не подтверждён")
            return Enrichment(fields={"inn": next(iter(inns))}, profile={"url": result.fetched[0]},
                              counters={"inn_found": 1})
        fields = {}
        if not inn:
            if not company.city:
                return Enrichment(status="skipped", error="нет города для однозначного поиска ИНН")
            try:
                token = dadata.DadataProvider().token
            except dadata.DadataNotConfigured:
                return Enrichment(status="skipped", error="DaData не настроена")
            async with ctx.http(Authorization=f"Token {token}") as http:
                response = await http.post(SUGGEST_URL, json={"query": f"{company.name or ''} {company.city}",
                                                            "count": 20, "branch_type": "MAIN"})
                response.raise_for_status()
                inn = unique_party(response.json().get("suggestions") or [], company.name, company.city)
            if not inn:
                return Enrichment(status="skipped", error="нет единственного совпадения названия и города")
            fields["inn"] = inn
        try:
            provider = provider_factory()
        except Exception:
            # A verified DaData identity remains useful even if website search is not configured.
            return Enrichment(status="done" if fields else "skipped", error="поиск XMLStock не настроен",
                              fields=fields, counters={"inn_found": int(bool(fields))})
        platforms = tuple((ctx.cache.get("leadgen_settings") or service.DEFAULT_SETTINGS)["platform_domains"])
        queries = [f'"{inn}"', f"{clean_company_name(company.name or '')[0]} {company.city or ''} официальный сайт"]
        seen = set()
        for query in queries:
            try:
                page = await provider.search_page(query.strip(), company.region_code or 225)
            except Exception:
                return Enrichment(status="done" if fields else "failed", error="поиск не ответил",
                                  fields=fields, counters={"inn_found": int(bool(fields))})
            for item in page.organic:
                domain = normalize_domain(item.url)
                excluded = is_platform_domain(domain, (*platforms, *JOB_BOARDS, *DIRECTORIES))
                if not public_host(domain) or domain in seen or excluded:
                    continue
                seen.add(domain)
                # Bound crawl work per company, even when the provider returns a very large page.
                if len(seen) > 5:
                    break
                result = await checked_site(domain, ctx)
                if result.ok and inn in result.facts.inns:
                    return Enrichment(fields={**fields, "domain": domain}, profile={"url": item.url},
                                      counters={"site_found": 1, "inn_found": int(bool(fields))})
        return Enrichment(status="done" if fields else "skipped", fields=fields,
                          error="сайт с совпадающим ИНН не найден", counters={"inn_found": int(bool(fields))})


ADAPTER = FindSiteSource()
