"""«Работа России» (opendata.trudvsem.ru) — без ключа. Рабочие профессии, медицина, госсектор, регионы."""
from __future__ import annotations

from datetime import datetime, timezone

from app.domains.jobradar.sources.base import Adapter, Context, SourceFormatError, Vacancy, limited, norm, parse_dt, \
    strip_tags, to_int
from app.domains.jobradar.sources.regions import codes_for

URL = "http://opendata.trudvsem.ru/api/v1/vacancies"
LIMIT = 100
MAX_PAGES = 5          # offset — номер страницы (0…99); 5 страниц по 100 на слово и регион — с запасом на 20 минут
PAUSE = 1.0
# Крупные сети по умолчанию: «Пятёрочка» (ООО «Агроторг»), «Магнит» (АО «Тандер»), ОАО «РЖД», «Билайн» (ПАО «ВымпелКом»),
# Fix Price (ООО «Бэст Прайс»).
DEFAULT_EXCLUDE_INN = ["7825706086", "2310031475", "7708503727", "7713076301", "5047085094"]


def experience_code(years) -> str:
    n = to_int(years) or 0
    return "noExperience" if n < 1 else "between1And3" if n < 3 else "between3And6" if n < 6 else "moreThan6"


def is_direct(v: dict) -> bool:
    """Вакансия напрямую от работодателя: не скопирована с другого сайта и не от кадрового агентства."""
    source = norm(v.get("source"))
    company = v.get("company") or {}
    return "интернет" not in source and not company.get("hr-agency")


def contacts(v: dict) -> dict:
    """Контакты, которые работодатель сам указал в вакансии (телефон, почта). ФИО не берём."""
    phones, emails = [], []
    for c in v.get("contact_list") or []:
        kind, value = norm(c.get("contact_type")), str(c.get("contact_value") or "").strip()
        if not value:
            continue
        if "почт" in kind or "mail" in kind or "@" in value:
            emails.append(value)
        elif "телефон" in kind or "phone" in kind:
            phones.append(value)
    if v.get("email"):
        emails.append(str(v["email"]).strip())
    out = {}
    if phones:
        out["phones"] = list(dict.fromkeys(phones))[:3]
    if emails:
        out["emails"] = list(dict.fromkeys(emails))[:3]
    return out


def parse(data: dict) -> list[Vacancy]:
    if not isinstance(data, dict) or ("results" not in data and "meta" not in data):
        raise SourceFormatError()
    out = []
    for wrap in ((data.get("results") or {}).get("vacancies") or []):   # пусто: 200, meta.total=0, без vacancies
        v = wrap.get("vacancy") or {}
        if not v.get("id"):
            continue
        company = v.get("company") or {}
        region = v.get("region") or {}
        schedule = norm(v.get("schedule"))
        requirement = v.get("requirement") or {}
        code = str(region.get("region_code") or "")
        out.append(Vacancy(
            source="trudvsem", ext_id=str(v["id"]), title=v.get("job-name") or "",
            company=company.get("name") or "", company_inn=(company.get("inn") or None),
            city=region.get("name") or "", region_code=to_int(code[:2]) if code else None,
            salary_from=to_int(v.get("salary_min")), salary_to=to_int(v.get("salary_max")),
            remote="удал" in schedule or "дистанц" in schedule,
            experience=experience_code(requirement.get("experience")) if "experience" in requirement else "",
            url=v.get("vac_url") or "",
            snippet=strip_tags(" ".join(filter(None, [v.get("duty"), requirement.get("qualification")])))[:400],
            published=parse_dt(v.get("date_modify") or v.get("modified") or v.get("creation-date")),
            # company.url — страница компании на trudvsem.ru, не сайт; сайт только из site
            employer={"inn": company.get("inn") or None, "site": company.get("site") or None,
                      "url": company.get("url") or None, **contacts(v)},
            extra={"direct": is_direct(v), "category": (v.get("category") or {}).get("specialisation")},
        ))
    return out


class TrudvsemAdapter(Adapter):
    code = "trudvsem"
    title = "Работа России"
    icon = "🇷🇺"
    purpose = "Рабочие профессии, медицина, госсектор"
    settings_schema = [
        {"key": "exclude_inn", "label": "Исключить работодателей по ИНН", "type": "list", "default": DEFAULT_EXCLUDE_INN,
         "hint": "По умолчанию — крупные сети: Пятёрочка, Магнит, РЖД, Билайн, Fix Price"},
        {"key": "direct_only", "label": "Только вакансии напрямую от работодателей", "type": "bool", "default": False,
         "hint": "Без скопированных с других сайтов и без кадровых агентств"},
    ]

    unit_seconds = 2.5   # запрос + пауза 1 с
    concurrency = 3      # API медленный (до 8–9 с на ответ): три запроса разом, каждый со своей паузой

    def units(self, f, filter_settings, settings) -> int:   # слово × регион, первая страница
        codes, _unknown = codes_for(f.get("regions") or [])
        return max(1, len(f.get("keywords") or [])) * max(1, len(codes))

    async def fetch(self, f, since, settings, filter_settings, ctx: Context) -> list[Vacancy]:
        codes, _unknown = codes_for(f.get("regions") or [])
        exclude = {str(i).strip() for i in (settings or {}).get("exclude_inn") or [] if str(i).strip()}
        direct_only = bool((settings or {}).get("direct_only"))
        paths = [f"{URL}/region/{code:02d}" for code in codes] or [URL]
        jobs = [(keyword, path) for keyword in f.get("keywords") or [] for path in paths]
        modified = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with ctx.http() as http:
            async def one(job) -> list[Vacancy]:
                keyword, path = job
                got: list[Vacancy] = []
                for page in range(MAX_PAGES):
                    if page:
                        await ctx.sleep(PAUSE)
                    # httpx кодирует кириллицу в text сам (без кодирования API отвечает 400)
                    response = await http.get(path, params={"text": keyword, "limit": LIMIT, "offset": page,
                                                            "modifiedFrom": modified})
                    response.raise_for_status()
                    items = parse(response.json())
                    got += [v for v in items if not (v.company_inn and v.company_inn in exclude)
                            and not (direct_only and not v.extra.get("direct"))]
                    if len(items) < LIMIT:
                        break
                return got
            pages = await limited(ctx, jobs, one, self.concurrency, PAUSE)
        return [v for got in pages for v in got]

    async def healthcheck(self, settings, ctx):
        try:
            async with ctx.http() as http:
                response = await http.get(URL, params={"text": "маркетолог", "limit": 1, "offset": 0})
                response.raise_for_status()
                total = ((response.json().get("meta") or {}).get("total"))
            return True, f"отвечает, всего вакансий по слову «маркетолог»: {total if total is not None else '?'}"
        except Exception as exc:  # noqa: BLE001
            from app.domains.jobradar.sources.base import short_error
            return False, short_error(exc)


ADAPTER = TrudvsemAdapter()
