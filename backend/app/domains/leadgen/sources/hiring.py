"""«Нанимают в нише» (finder): компании с открытыми вакансиями ниши на площадках радара вакансий.

Площадки — адаптеры радара (jobradar/sources: trudvsem, hh) с их настройками с плашек радара (jobradar/platforms.py):
токен hh и список ИНН-исключений второй раз не вводятся. Одна компания — одна находка; сети (ИНН-исключения
«Работы России») и кадровые агентства отсекаются. Сигнал hiring по первой вакансии.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from app.domains.jobradar import core as jr_core
from app.domains.jobradar import platforms as jr_platforms
from app.domains.jobradar.sources import REGISTRY as JR
from app.domains.jobradar.sources import hh as jr_hh
from app.domains.jobradar.sources.base import Vacancy, norm
from app.domains.leadgen import service
from app.domains.leadgen.site_enrich import _ROLE_BOXES
from app.domains.leadgen.sources.base import Candidate, CompanySource, Context, SignalIn, SourceSkipped, limited, \
    short_error

PLATFORMS = ("trudvsem", "hh")
AGENCY_WORDS = ("кадров", "рекрут", "recruit", "персонал", "staff", "аутстаф", "аутсорсинг персонала",
                "подбор персонала", "hr-агентств", "hr агентств", "кадровое агентство")
HH_PARAMS = {"text", "area", "professional_role", "industry", "salary", "currency", "only_with_salary", "experience",
             "employment", "schedule", "work_format", "search_field", "label", "education", "order_by", "period",
             "employer_id", "metro", "part_time", "accept_temporary"}
HH_PAGES = 5
EMPLOYER_SITES = 40          # сайт работодателя с hh — не больше стольких компаний за запуск
SIGNAL_TTL = 30


def parse_hh_url(url: str) -> list[tuple[str, str]] | None:
    """Ссылка поиска hh.ru -> параметры API /vacancies. None — это не ссылка поиска вакансий hh."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if not (host == "hh.ru" or host.endswith(".hh.ru")) or "/search/vacancy" not in parts.path:
        return None
    out = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        key = "period" if key == "search_period" else key
        if key in HH_PARAMS and value.strip():
            out.append((key, value.strip()))
    if not any(k in ("text", "professional_role", "employer_id", "industry") for k, _ in out):
        return None
    return out


def is_agency(v: Vacancy) -> bool:
    name = norm(v.company)
    return any(w in name for w in AGENCY_WORDS)


def general_emails(emails: list[str]) -> list[str]:
    """Только общие ящики (info@, hr@ не берём как личные — берём role-ящики компании); именные адреса не храним."""
    out = []
    for email in emails or []:
        local = email.split("@")[0].split("+")[0].lower()
        if any(local == r or local.startswith(r) for r in _ROLE_BOXES):
            out.append(email.strip().lower())
    return out


class HiringSource(CompanySource):
    code = "hiring"
    kind = "finder"
    title = "Нанимают в нише"
    icon = "🧑‍💼"
    purpose = "Компании, у которых сейчас открыты вакансии ниши (Работа России, hh.ru)"
    unit_seconds = 2.5
    concurrency = 1           # площадки опрашиваем по очереди, у каждой свой лимит внутри адаптера
    query_schema = [
        {"key": "titles", "type": "list", "label": "Кого ищут (названия вакансий)", "required": True,
         "ai": {"purpose": "jobs"}},
        {"key": "region", "type": "region", "label": "Город или регион", "required": True},
        {"key": "days", "type": "number", "label": "Вакансии за последние, дней", "default": 14, "min": 1, "max": 30},
        {"key": "platforms", "type": "multi", "label": "Площадки", "default": ["trudvsem", "hh"],
         "options": [{"value": "trudvsem", "label": "Работа России"}, {"value": "hh", "label": "hh.ru"}]},
        {"key": "hh_url", "type": "text", "label": "Или ссылка поиска hh.ru",
         "hint": "Скопируйте адрес страницы поиска на hh.ru — возьмём её условия вместо полей выше"},
    ]
    signals_info = [{"kind": "hiring", "label": "Нанимает (радар вакансий)"}]

    def missing(self, query: dict) -> str | None:
        if query.get("hh_url"):     # ссылка hh задаёт и должности, и город сама
            return None
        return super().missing(query)

    def problem(self, query: dict) -> str | None:
        if query.get("hh_url") and parse_hh_url(query["hh_url"]) is None:
            return "Это не ссылка поиска вакансий hh.ru"
        if not query.get("platforms"):
            return "Выберите хотя бы одну площадку"
        if query.get("hh_url") and "hh" not in query["platforms"]:
            return "Ссылка hh.ru указана, но площадка hh.ru не выбрана"
        return None

    def _filter(self, query: dict) -> dict:
        return {"keywords": query.get("titles") or [], "exclude": [], "regions": [query["region"]] if query.get("region") else [],
                "strict_city": True, "mode": "leads", "allow_no_salary": True}

    def units(self, query, filter_settings, settings) -> int:
        f = self._filter(query)
        total = 0
        for code in query.get("platforms") or []:
            if code == "trudvsem" and f["keywords"]:
                total += JR["trudvsem"].units(f, {}, {})
            elif code == "hh":
                total += 2 + min(EMPLOYER_SITES, 20)
        return max(1, total)

    async def fetch(self, query: dict, settings: dict, ctx: Context) -> list[Candidate]:
        errors = ctx.cache.setdefault("errors", {})
        radar = await _radar_settings(query.get("platforms") or [])
        since = datetime.now(timezone.utc) - timedelta(days=int(query.get("days") or 14))
        f = self._filter(query)
        found: list[Vacancy] = []
        ok_any = False
        for code in query.get("platforms") or []:
            adapter = JR.get(code)
            enabled, plat = radar.get(code, (False, {}))
            if adapter is None:
                continue
            title = adapter.title
            if not enabled:
                errors[code] = f"{title}: выключена в радаре вакансий"
                continue
            reason = adapter.ready(plat)
            if reason:
                errors[code] = f"{title}: {reason}"
                continue
            try:
                if code == "hh" and query.get("hh_url"):
                    items = await _hh_by_url(parse_hh_url(query["hh_url"]) or [], since, plat, ctx)
                elif not f["keywords"]:
                    errors[code] = f"{title}: не указаны названия вакансий"
                    continue
                else:
                    items = [v for v in await adapter.fetch(f, since, plat, {}, ctx) if jr_core.matches(v, f)[0]]
            except SourceSkipped as skip:
                errors[code] = f"{title}: {skip}"
                continue
            except Exception as exc:  # noqa: BLE001 — одна площадка не валит поиск
                errors[code] = f"{title}: {short_error(exc)}"
                continue
            ok_any = True
            found += items
        if not ok_any:
            raise SourceSkipped("; ".join(errors.values()) or "нет доступных площадок")

        exclude = {str(i).strip() for i in (radar.get("trudvsem", (False, {}))[1].get("exclude_inn") or [])}
        groups: dict[str, list[Vacancy]] = {}
        for v in found:
            inn = v.company_inn or v.employer.get("inn")
            if not (v.company or "").strip() or (inn and inn in exclude) or is_agency(v):
                continue
            if v.source == "trudvsem" and not v.extra.get("direct", True):
                continue    # скопирована с другого сайта или от кадрового агентства
            key = f"inn:{inn}" if inn else f"hh:{v.employer['id']}" if v.employer.get("id") else \
                f"name:{norm(v.company)}|{norm(v.city)}"
            groups.setdefault(key, []).append(v)

        hh_plat = radar.get("hh", (False, {}))[1]
        need_site = [cards for cards in groups.values()
                     if any(c.source == "hh" and c.employer.get("id") for c in cards)][:EMPLOYER_SITES]
        sites: dict[str, str | None] = {}

        async def site(cards):
            employer_id = next(c.employer["id"] for c in cards if c.source == "hh" and c.employer.get("id"))
            try:
                sites[employer_id] = await JR["hh"].employer_site(employer_id, hh_plat, ctx)
            except Exception:  # noqa: BLE001 — без сайта компания всё равно попадёт
                sites[employer_id] = None
        if need_site and hh_plat.get("token"):
            await limited(ctx, need_site, site, 2, 0.3)
        return [self._candidate(cards, sites) for cards in groups.values()]

    @staticmethod
    def _candidate(cards: list[Vacancy], sites: dict) -> Candidate:
        main = next((c for c in cards if c.employer.get("id")), cards[0])
        first = sorted(cards, key=lambda c: c.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[0]
        inn = next((c.company_inn or c.employer.get("inn") for c in cards if c.company_inn or c.employer.get("inn")), None)
        site = next((c.employer.get("site") for c in cards if c.employer.get("site")), None) or \
            sites.get(main.employer.get("id"))
        phones = list(dict.fromkeys(p for c in cards for p in c.employer.get("phones") or []))[:3]
        emails = general_emails([e for c in cards for e in c.employer.get("emails") or []])[:3]
        finding = service.FindingIn(
            source="hh" if main.source == "hh" else "trudvsem", name=main.company, inn=inn, domain=site,
            city=None if main.remote else (main.city or None), phones=phones,
            contacts=[{"kind": "email", "value": e} for e in dict.fromkeys(emails)],
            hh_employer_id=main.employer.get("id") if main.source == "hh" else None,
            source_url=main.employer.get("url") or main.url)
        signal = SignalIn("hiring", {"title": first.title, "url": first.url, "platform": first.source,
                                     "vacancies": len(cards)}, ttl_days=SIGNAL_TTL)
        return Candidate(finding, [signal])


async def _radar_settings(codes: list[str]) -> dict[str, tuple[bool, dict]]:
    """Плашки радара: (включена, настройки с расшифрованными ключами) — ключи не копируем к себе."""
    from app.domains.leadgen import routes
    async with routes.session_factory() as db:
        rows = await jr_platforms.ensure(db)
        await db.commit()
        out = {}
        for code in codes:
            row, adapter = rows.get(code), JR.get(code)
            if row is not None and adapter is not None:
                out[code] = (bool(row.enabled), jr_platforms.plain(adapter, row.settings))
        return out


async def _hh_by_url(params: list[tuple[str, str]], since: datetime, settings: dict, ctx: Context) -> list[Vacancy]:
    """Поиск hh по условиям из ссылки (тот же API и разбор, что у адаптера радара)."""
    if not settings.get("token"):
        raise SourceSkipped(jr_hh.TOKEN_HINT)
    params = [p for p in params if p[0] not in ("per_page", "page")] + [("per_page", "100")]
    if not any(k == "period" for k, _ in params):
        params.append(("date_from", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")))
    out: list[Vacancy] = []
    async with ctx.http(**JR["hh"].headers(settings)) as http:
        for page in range(HH_PAGES):
            response = await http.get(jr_hh.URL, params=params + [("page", str(page))])
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or "items" not in data:
                from app.domains.jobradar.sources.base import SourceFormatError
                raise SourceFormatError()
            out += [jr_hh.parse_item(item) for item in data.get("items") or []]
            if page + 1 >= int(data.get("pages") or 0):
                break
    return out


ADAPTER = HiringSource()
