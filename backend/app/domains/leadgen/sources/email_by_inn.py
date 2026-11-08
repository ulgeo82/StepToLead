"""«Почта по ИНН» (enricher): общие почты и сайт компании из реестров по ИНН — для тех, у кого сайт почту не дал
или сайта нет вовсе (частые находки из вакансий).

Где ищем, по очереди, до первой почты:
1. DaData — тот же ключ, что у DaData в портале. Почты отдаются только на тарифе «Максимальный»;
   на других тарифах шаг просто ничего не находит (и денег не тратит сверх тарифа).
2. Checko (checko.ru) — ключ API на плашке источника. Платно за запрос, поэтому по умолчанию спрашиваем
   только компании без общей почты.

Именные адреса сотрудников (ivan.petrov@…) не сохраняем — как и с сайтов. Сайт из реестра ставим, только если
у компании его ещё нет; дальше «Найти почты» соберёт с него контакты.
"""
from __future__ import annotations

import re

from app.domains.leadgen import dadata as legacy_dadata
from app.domains.leadgen import site_enrich
from app.domains.leadgen.core.normalize import is_platform_domain, normalize_domain
from app.domains.leadgen.core.site_extract import good_email
from app.domains.leadgen.sources.base import CompanySource, Enrichment, short_error

CHECKO_URL = "https://api.checko.ru/v2/company"
CHECKO_IP_URL = "https://api.checko.ru/v2/entrepreneur"
CHECK_INN = "7707083893"            # для кнопки «Проверить»: крупное известное юрлицо
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}", re.I)
_SITE_KEYS = ("вебсайт", "сайт", "website", "site", "www")


def _walk(node, key=""):
    """Все пары (ключ, строка) ответа — формат реестров меняется, а почта и сайт узнаются по виду значения."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, str(k))
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    elif isinstance(node, str):
        yield key.casefold(), node


def contacts_from(data) -> tuple[list[str], list[str]]:
    """Почты и сайты из ответа DaData/Checko (data-часть). Почты — только похожие на настоящие, без мусора."""
    emails, sites = [], []
    for key, value in _walk(data or {}):
        for m in _EMAIL.finditer(value):
            e = m.group(0).lower().strip(".")
            if good_email(e) and e not in emails:
                emails.append(e)
        if any(s in key for s in _SITE_KEYS) and "@" not in value:
            domain = normalize_domain(value)
            if domain and "." in domain and domain not in sites:
                sites.append(domain)
    return emails, sites


class EmailByInnSource(CompanySource):
    code = "email_by_inn"
    title = "Почта по ИНН"
    icon = "📇"
    purpose = "Общие почты и сайт из реестров по ИНН (DaData «Максимальный», Checko) — когда на сайте почты нет"
    requires_site = False
    field_source = "site_search"   # сайт из реестра — не надёжнее найденного поиском с проверкой ИНН
    unit_seconds = 1.0
    concurrency = 4                 # платные API — вежливо
    fresh_days = 90                 # реестры меняются редко, а запрос стоит денег
    settings_schema = [
        {"key": "checko_key", "label": "Ключ API Checko", "type": "secret", "default": None,
         "hint": "checko.ru → Личный кабинет → API. Без ключа ищем только в DaData."},
        {"key": "use_dadata", "label": "Сначала искать в DaData", "type": "bool", "default": True,
         "hint": "Почты в DaData есть только на тарифе «Максимальный»"},
        {"key": "only_missing", "label": "Только компании без общей почты", "type": "bool", "default": True,
         "hint": "Не тратить запросы на тех, у кого почта уже есть"},
        {"key": "take_site", "label": "Ставить сайт из реестра, если сайта нет", "type": "bool", "default": True},
        {"key": "price_per_request", "label": "Цена запроса Checko, ₽", "type": "number", "default": None},
    ]

    def _dadata_token(self) -> str | None:
        from app.domains.leadgen import routes
        try:
            return routes.dadata_factory().token
        except Exception:  # noqa: BLE001 — DaData не настроена
            return None

    def ready(self, settings):
        if settings.get("checko_key") or (settings.get("use_dadata", True) and self._dadata_token()):
            return None
        return "Нужен ключ Checko или DaData в портале"

    def cost(self, query, settings):
        if not settings.get("checko_key"):
            return None
        requests = int(query.get("companies") or 0)
        price = settings.get("price_per_request")
        return {"requests": requests, "rub": round(requests * float(price), 2) if price else None}

    async def _dadata(self, inn, token, ctx):
        async with ctx.http(Authorization=f"Token {token}", Accept="application/json") as client:
            resp = await client.post(legacy_dadata.URL, json={"query": inn, "branch_type": "MAIN", "count": 1})
            resp.raise_for_status()
            items = resp.json().get("suggestions") or []
            return items[0].get("data") if items else None

    async def _checko(self, inn, key, ctx):
        url = CHECKO_IP_URL if len(inn) == 12 else CHECKO_URL
        async with ctx.http(Accept="application/json") as client:
            resp = await client.get(url, params={"key": key, "inn": inn})
            if resp.status_code in (401, 403):
                raise PermissionError("Checko не принял ключ")
            resp.raise_for_status()
            body = resp.json()
        meta = body.get("meta") or {}
        if str(meta.get("status", "ok")).lower() not in ("ok", "success"):
            raise RuntimeError("Checko: " + str(meta.get("message") or "ошибка")[:120])
        return body.get("data"), meta

    async def enrich(self, company, settings, ctx):
        if not company.inn:
            return Enrichment(status="skipped", error="нет ИНН")
        if settings.get("only_missing", True) and any(
                c.get("kind") == "email" and not c.get("is_personal") for c in company.contacts):
            return Enrichment(status="skipped", error="почта уже есть")
        tried, errors, emails, sites, used = [], [], [], [], None
        token = self._dadata_token() if settings.get("use_dadata", True) else None
        if token:
            tried.append("dadata")
            try:
                emails, sites = contacts_from(await self._dadata(company.inn, token, ctx))
                used = "dadata" if emails else None
            except Exception as exc:  # noqa: BLE001
                errors.append("DaData: " + short_error(exc))
        meta = {}
        if not emails and settings.get("checko_key"):
            tried.append("checko")
            try:
                data, meta = await self._checko(company.inn, settings["checko_key"], ctx)
                emails, more_sites = contacts_from(data)
                sites += [s for s in more_sites if s not in sites]
                used = "checko" if emails else None
            except PermissionError as exc:
                return Enrichment(status="failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                errors.append("Checko: " + short_error(exc))
        if not tried:
            return Enrichment(status="skipped", error="нет ключа Checko и DaData")
        shared = [e for e in emails if not site_enrich._looks_personal(e, company.domain or "")]
        enr = Enrichment(profile={"emails": shared, "sites": sites, "source": used, "tried": tried,
                                  "personal_dropped": len(emails) - len(shared)},
                         counters={"emails": len(shared), "personal_dropped": len(emails) - len(shared)})
        if meta.get("balance") is not None:
            enr.profile["checko_balance"] = meta.get("balance")
        site = next((s for s in sites if not is_platform_domain(s)), None)
        if site and not company.domain and settings.get("take_site", True):
            enr.fields["domain"] = site
            enr.counters["sites"] = 1
        enr.contacts = [{"kind": "email", "value": e} for e in shared]
        if not shared:
            if errors and len(errors) == len(tried):
                return Enrichment(status="failed", error="; ".join(errors)[:200])
            enr.status = "skipped" if not enr.fields else "done"
            enr.error = "в реестрах почты нет" if not enr.fields else None
        return enr

    async def healthcheck(self, settings, ctx):
        parts = []
        token = self._dadata_token() if settings.get("use_dadata", True) else None
        if token:
            try:
                emails, _ = contacts_from(await self._dadata(CHECK_INN, token, ctx))
                parts.append("DaData отвечает" + ("" if emails else ", но почты не отдаёт (нужен тариф «Максимальный»)"))
            except Exception as exc:  # noqa: BLE001
                parts.append("DaData: " + short_error(exc))
        if settings.get("checko_key"):
            try:
                data, meta = await self._checko(CHECK_INN, settings["checko_key"], ctx)
                emails, _ = contacts_from(data)
                balance = meta.get("balance")
                parts.append("Checko отвечает" + (f", баланс {balance}" if balance is not None else "")
                             + ("" if emails else ", но почт в ответе не нашли — проверьте тариф"))
            except Exception as exc:  # noqa: BLE001
                return False, "Checko: " + (str(exc) if isinstance(exc, (PermissionError, RuntimeError))
                                            else short_error(exc))
        if not parts:
            return False, "Нужен ключ Checko или DaData в портале"
        return True, "; ".join(parts)


ADAPTER = EmailByInnSource()
