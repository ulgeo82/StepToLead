"""SuperJob — включается только с ключом приложения (X-Api-App-Id, api.superjob.ru).

Лимиты: 120 запросов в минуту, 500 результатов на запрос (5 страниц по 100).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.domains.jobradar.sources.base import Adapter, Context, SourceFormatError, SourceSkipped, Vacancy, strip_tags, \
    to_int

URL = "https://api.superjob.ru/2.0/vacancies/"
PAGES = 5
COUNT = 100
PAUSE = 0.6            # < 120 запросов в минуту
KEY_HINT = "нужен ключ приложения (api.superjob.ru)"
EXPERIENCE = {1: "noExperience", 2: "between1And3", 3: "between3And6", 4: "moreThan6"}


def parse(data: dict) -> list[Vacancy]:
    if not isinstance(data, dict) or "objects" not in data:
        raise SourceFormatError()
    out = []
    for item in data.get("objects") or []:
        if not item.get("id"):
            continue
        published = item.get("date_published")
        client = item.get("client") or {}
        out.append(Vacancy(
            source="superjob", ext_id=str(item["id"]), title=item.get("profession") or "",
            company=item.get("firm_name") or client.get("title") or "",
            city=(item.get("town") or {}).get("title") or "",
            salary_from=to_int(item.get("payment_from")), salary_to=to_int(item.get("payment_to")),
            currency="RUR" if (item.get("currency") or "rub").lower() == "rub" else str(item.get("currency")).upper(),
            remote=(item.get("place_of_work") or {}).get("id") == 2,
            experience=EXPERIENCE.get((item.get("experience") or {}).get("id"), ""),
            url=item.get("link") or "", snippet=strip_tags(item.get("candidat") or item.get("vacancyRichText"))[:400],
            published=datetime.fromtimestamp(int(published), timezone.utc) if published else None,
            employer={"id": str(client["id"]) if client.get("id") else None, "url": client.get("link") or None,
                      "site": client.get("url") or None},
        ))
    return out


class SuperjobAdapter(Adapter):
    code = "superjob"
    title = "SuperJob"
    icon = "🟦"
    purpose = "Все профессии, сильна в регионах"
    needs_key = True
    key_field = "app_id"
    default_enabled = False
    settings_schema = [{"key": "app_id", "label": "Ключ приложения (X-Api-App-Id)", "type": "secret",
                        "hint": "api.superjob.ru → «Зарегистрировать приложение»"}]

    unit_seconds = 1.5

    def units(self, f, filter_settings, settings) -> int:   # слово × город
        towns = [t for t in f.get("regions") or [] if t.strip()] or [None]
        return max(1, len(f.get("keywords") or [])) * len(towns)

    def ready(self, settings):
        return None if (settings or {}).get("app_id") else KEY_HINT

    async def fetch(self, f, since, settings, filter_settings, ctx: Context) -> list[Vacancy]:
        if not (settings or {}).get("app_id"):
            raise SourceSkipped(KEY_HINT)
        towns = [t for t in f.get("regions") or [] if t.strip()] or [None]
        out: list[Vacancy] = []
        first = True
        async with ctx.http(**{"X-Api-App-Id": settings["app_id"]}) as http:
            for keyword in f.get("keywords") or []:
                for town in towns:
                    for page in range(PAGES):
                        if not first:
                            await ctx.sleep(PAUSE)
                        first = False
                        params = {"keyword": keyword, "count": COUNT, "page": page,
                                  "date_published_from": int(since.timestamp()), "order_field": "date"}
                        if town:
                            params["town"] = town
                        if f.get("salary_from"):
                            params["payment_from"] = int(f["salary_from"])
                            if not f.get("allow_no_salary", True):
                                params["no_agreement"] = 1
                        if f.get("remote_only"):
                            params["place_of_work"] = 2
                        response = await http.get(URL, params=params)
                        response.raise_for_status()
                        data = response.json()
                        out += parse(data)
                        if not data.get("more"):
                            break
        return out


ADAPTER = SuperjobAdapter()
