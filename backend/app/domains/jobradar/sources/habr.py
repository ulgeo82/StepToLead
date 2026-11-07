"""Хабр Карьера (RSS) — без ключа. IT и digital. Зарплаты и опыта в RSS нет: salary_known=False."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from app.domains.jobradar.sources.base import Adapter, Context, SourceFormatError, Vacancy, parse_rfc822, strip_tags

URL = "https://career.habr.com/vacancies/rss"
PAUSE = 1.0


def split_title(title: str) -> tuple[str, str]:
    """«Требуется «Должность» (Компания)» -> («Должность», «Компания»)."""
    title = strip_tags(title)
    company = ""
    stripped = title.rstrip()
    if stripped.endswith(")"):   # компания в последних скобках, внутри могут быть свои скобки: «(ООО (Альфа))»
        depth = 0
        for i in range(len(stripped) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(stripped[i], 0)
            if depth == 0:
                company, title = stripped[i + 1:-1].strip(), stripped[:i].strip()
                break
    title = re.sub(r"^Требуется\s*", "", title).strip("«»\"„“ ")
    return title, company


def parse(content: bytes, *, remote: bool = False) -> list[Vacancy]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise SourceFormatError() from exc
    if root.find("channel") is None:
        raise SourceFormatError()
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        number = re.search(r"/vacancies/(\d+)", link)
        title, company = split_title(item.findtext("title") or "")
        out.append(Vacancy(source="habr", ext_id=number.group(1) if number else link[-160:], title=title,
                           company=company, remote=remote, url=link, salary_known=False,
                           snippet=strip_tags(item.findtext("description"))[:400],
                           published=parse_rfc822(item.findtext("pubDate"))))
    return out


class HabrAdapter(Adapter):
    code = "habr"
    title = "Хабр Карьера"
    icon = "💻"
    purpose = "IT и digital"
    # Квалификацию (qid) не делаем, пока не подтвердим живым запросом, что RSS её учитывает.

    async def fetch(self, f, since, settings, filter_settings, ctx: Context) -> list[Vacancy]:
        out: list[Vacancy] = []
        async with ctx.http() as http:
            for i, keyword in enumerate(f.get("keywords") or []):
                if i:
                    await ctx.sleep(PAUSE)
                params = {"q": keyword, "type": "all"}
                if f.get("remote_only"):
                    params["remote"] = "true"
                response = await http.get(URL, params=params)
                response.raise_for_status()
                out += parse(response.content, remote=bool(f.get("remote_only")))
        return [v for v in out if not v.published or v.published >= since]


ADAPTER = HabrAdapter()
