"""FL.ru public RSS. Project budgets are not salaries."""
import re
import xml.etree.ElementTree as ET
from datetime import timezone
from urllib.parse import urlsplit

from .base import Adapter, Context, SourceFormatError, Vacancy, has_word, parse_rfc822, strip_tags

RSS = "https://www.fl.ru/rss/all.xml"


def project_details(description: str) -> dict:
    text = strip_tags(description).replace("\xa0", " ")
    budget = re.search(r"(?:бюджет|стоимость|оплата)\s*[:—–-]?\s*(?:от\s+)?"
                       r"(\d[\d ]*(?:[.,]\d+)?)\s*(тыс(?:\.|яч[аи]?)?)?\s*(?:₽|руб\.?|р\.)", text, re.I)
    amount = None
    if budget:
        amount = int(float(budget[1].replace(" ", "").replace(",", ".")) * (1000 if budget[2] else 1))
    deadline = re.search(r"(?:срок(?:\s+выполнения)?|дедлайн)\s*[:—–-]\s*([^.;\n]{1,100})", text, re.I)
    return {"budget": amount, "deadline": deadline[1].strip() if deadline else None}


def parse_feed(content: str) -> list[Vacancy]:
    try:
        root = ET.fromstring(content.lstrip())
    except ET.ParseError as error:
        raise SourceFormatError() from error
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise SourceFormatError()
    result = []
    for item in channel.findall("item"):
        title = strip_tags(item.findtext("title"))
        url = (item.findtext("link") or "").strip()
        parsed = urlsplit(url)
        if not title or parsed.scheme not in {"http", "https"} or parsed.hostname not in {"fl.ru", "www.fl.ru"}:
            raise SourceFormatError()
        description = strip_tags(item.findtext("description"))
        identifier = re.search(r"/projects/(\d+)", parsed.path)
        published = parse_rfc822(item.findtext("pubDate"))
        if published and published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        result.append(Vacancy(source="flru", ext_id=identifier[1] if identifier else url,
                              title=title, kind="project", url=url, snippet=description[:300],
                              salary_known=False, remote=True,
                              published=published,
                              extra={**project_details(item.findtext("description") or ""), "text": description}))
    return result


class FlruAdapter(Adapter):
    code = "flru"
    title = "FL.ru"
    icon = "🧩"
    purpose = "Фриланс-проекты"
    filter_schema = [
        {"key": "include_projects", "label": "Включать проекты", "type": "bool", "default": False},
        {"key": "budget_from", "label": "Бюджет от, ₽", "type": "number", "default": 0},
    ]

    unit_seconds = 3.0

    def units(self, f, filter_settings, settings) -> int:   # одна RSS-лента на проход
        return 1

    async def fetch(self, f, since, settings, filter_settings, ctx: Context):
        if not filter_settings.get("include_projects", False):
            return []
        if "flru" not in ctx.cache:
            async with ctx.http() as http:
                response = await http.get(RSS)
                response.raise_for_status()
            ctx.cache["flru"] = parse_feed(response.text)
        floor = max(0, float(filter_settings.get("budget_from") or 0))
        result = []
        for vacancy in ctx.cache["flru"]:
            text = f"{vacancy.title} {vacancy.extra['text']}"
            if f.get("keywords") and not has_word(text, f["keywords"]):
                continue
            if has_word(text, f.get("exclude") or []):
                continue
            if vacancy.published and vacancy.published < since:
                continue
            if vacancy.extra["budget"] is not None and vacancy.extra["budget"] < floor:
                continue
            result.append(vacancy)
        return result

    async def healthcheck(self, settings, ctx):
        from .base import short_error
        try:
            async with ctx.http() as http:
                response = await http.get(RSS)
                response.raise_for_status()
            items = parse_feed(response.text)
            return True, f"RSS отвечает, проектов: {len(items)}"
        except Exception as error:
            return False, short_error(error)


ADAPTER = FlruAdapter()
