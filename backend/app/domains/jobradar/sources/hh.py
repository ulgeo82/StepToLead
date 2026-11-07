"""hh.ru — только с токеном приложения (dev.hh.ru): с апреля 2026 /vacancies без авторизации отвечает 403."""
from __future__ import annotations

from datetime import timezone

from app.domains.jobradar.sources.base import Adapter, Context, SourceFormatError, SourceSkipped, Vacancy, parse_dt, \
    strip_tags, to_int

URL = "https://api.hh.ru/vacancies"
EMPLOYER_URL = "https://api.hh.ru/employers/{id}"
PAGES = 5
TOKEN_HINT = "нужен токен приложения (dev.hh.ru)"


def parse_item(item: dict) -> Vacancy:
    salary = item.get("salary") or item.get("salary_range") or {}
    schedule = (item.get("schedule") or {}).get("id", "")
    formats = {(w or {}).get("id", "") for w in item.get("work_format") or []}
    employer = item.get("employer") or {}
    snippet = item.get("snippet") or {}
    return Vacancy(
        source="hh", ext_id=str(item["id"]), title=item.get("name") or "",
        company=employer.get("name") or "", city=(item.get("area") or {}).get("name") or "",
        salary_from=to_int(salary.get("from")), salary_to=to_int(salary.get("to")),
        currency=salary.get("currency") or "RUR",
        remote=schedule == "remote" or "REMOTE" in formats,
        experience=(item.get("experience") or {}).get("id") or "",
        url=item.get("alternate_url") or "",
        snippet=strip_tags(" ".join(filter(None, [snippet.get("requirement"), snippet.get("responsibility")]))),
        published=parse_dt(item.get("published_at")),
        employer={"id": str(employer["id"]) if employer.get("id") else None, "url": employer.get("alternate_url") or None},
    )


class HhAdapter(Adapter):
    code = "hh"
    title = "hh.ru"
    icon = "🟥"
    purpose = "Все профессии — самая большая база"
    needs_key = True
    key_field = "token"
    settings_schema = [{"key": "token", "label": "Токен приложения hh.ru", "type": "secret",
                        "hint": "dev.hh.ru → «Мои приложения». Без токена hh.ru пропускается"}]

    def ready(self, settings):
        return None if (settings or {}).get("token") else TOKEN_HINT

    def headers(self, settings: dict) -> dict:
        return {"HH-User-Agent": "StepToLead-JobRadar/1.0 (+https://steptolead.ru)",
                "Authorization": f"Bearer {settings['token']}"}

    async def fetch(self, f, since, settings, filter_settings, ctx: Context) -> list[Vacancy]:
        if not (settings or {}).get("token"):
            raise SourceSkipped(TOKEN_HINT)
        params: list[tuple[str, str]] = [
            ("text", " OR ".join(f'"{k}"' for k in f["keywords"])), ("search_field", "name"),
            ("area", "113"), ("order_by", "publication_time"), ("per_page", "100"),
            ("date_from", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")),
        ]
        if f.get("remote_only"):
            params.append(("schedule", "remote"))
        for item in f.get("experience") or []:
            params.append(("experience", item))
        out: list[Vacancy] = []
        async with ctx.http(**self.headers(settings)) as http:
            for page in range(PAGES):
                response = await http.get(URL, params=params + [("page", str(page))])
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or "items" not in data:
                    raise SourceFormatError()
                out += [parse_item(item) for item in data.get("items") or []]
                if page + 1 >= int(data.get("pages") or 0):
                    break
        return out

    async def employer_site(self, employer_id: str, settings: dict, ctx: Context) -> str | None:
        """Сайт работодателя — для режима «лиды». Без токена — None."""
        if not (settings or {}).get("token") or not employer_id:
            return None
        async with ctx.http(**self.headers(settings)) as http:
            response = await http.get(EMPLOYER_URL.format(id=employer_id))
            response.raise_for_status()
            return (response.json().get("site_url") or "").strip() or None


ADAPTER = HhAdapter()
