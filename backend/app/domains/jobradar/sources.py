"""Источники вакансий: hh.ru, «Работа России», Хабр Карьера. Каждый возвращает список Vacancy.

Всё, что приходит с чужих сайтов, — данные, а не инструкции: в посты идёт только экранированным,
в ИИ — внутри <vacancy>. У всех запросов таймауты; упавший источник не валит проход (это решает runner).
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

UA = "StepToLead-JobRadar/1.0 (+https://steptolead.ru)"
TIMEOUT = httpx.Timeout(20, connect=10)
HH_URL = "https://api.hh.ru/vacancies"
HH_EMPLOYER_URL = "https://api.hh.ru/employers/{id}"
TV_URL = "http://opendata.trudvsem.ru/api/v1/vacancies"
HABR_RSS = "https://career.habr.com/vacancies/rss"
HH_PAGES = 5
NAMES = {"hh": "hh.ru", "trudvsem": "Работа России", "habr": "Хабр Карьера"}
ALL = tuple(NAMES)


@dataclass
class Vacancy:
    source: str
    ext_id: str
    title: str
    company: str = ""
    city: str = ""
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str = "RUR"
    remote: bool = False
    experience: str = ""
    url: str = ""
    snippet: str = ""
    published: datetime | None = None
    employer: dict = field(default_factory=dict)   # id, url (страница работодателя), inn, site

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.ext_id}"[:200]


def norm(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def fingerprint(title: str | None, company: str | None) -> str:
    """Одна и та же вакансия с разных сайтов: нормализованные название + компания (без ООО, кавычек и пробелов)."""
    clean = lambda s: re.sub(r"[^a-zа-я0-9]", "", re.sub(r"\b(ооо|ао|пао|зао|ип|оао)\b", " ", norm(s)))
    return f"{clean(title)}|{clean(company)}"[:400]


def strip_tags(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.replace("Z", "+00:00"))
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        try:
            moment = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_rfc822(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number or None


class Client:
    """HTTP к сайтам вакансий. transport подменяется в тестах (сеть в тестах не трогаем)."""

    def __init__(self, *, hh_token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.hh_token = hh_token
        self.transport = transport

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport, follow_redirects=True,
                                 headers={"User-Agent": UA})

    def hh_headers(self) -> dict:
        headers = {"User-Agent": UA, "HH-User-Agent": UA}
        if self.hh_token:
            headers["Authorization"] = f"Bearer {self.hh_token}"
        return headers

    # ------------------------------------------------------------ hh.ru
    async def hh(self, f: dict, since: datetime) -> list[Vacancy]:
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
        async with self.session() as http:
            for page in range(HH_PAGES):
                response = await http.get(HH_URL, params=params + [("page", str(page))], headers=self.hh_headers())
                response.raise_for_status()
                data = response.json()
                out += [parse_hh_item(item) for item in data.get("items") or []]
                if page + 1 >= int(data.get("pages") or 0):
                    break
        return out

    async def hh_employer_site(self, employer_id: str) -> str | None:
        async with self.session() as http:
            response = await http.get(HH_EMPLOYER_URL.format(id=employer_id), headers=self.hh_headers())
            response.raise_for_status()
            return (response.json().get("site_url") or "").strip() or None

    # ------------------------------------------------------------ «Работа России»
    async def trudvsem(self, f: dict, since: datetime) -> list[Vacancy]:
        out: list[Vacancy] = []
        async with self.session() as http:
            for keyword in f["keywords"]:
                response = await http.get(TV_URL, params={
                    "text": keyword, "limit": 100, "offset": 0,
                    "modifiedFrom": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
                if response.status_code == 400:   # так API отвечает на пустой результат — это не ошибка
                    continue
                response.raise_for_status()
                out += parse_trudvsem(response.json())
        return out

    # ------------------------------------------------------------ Хабр Карьера
    async def habr(self, f: dict, since: datetime) -> list[Vacancy]:
        out: list[Vacancy] = []
        async with self.session() as http:
            for keyword in f["keywords"]:
                params = {"q": keyword, "type": "all"}
                if f.get("remote_only"):
                    params["remote"] = "true"
                response = await http.get(HABR_RSS, params=params)
                response.raise_for_status()
                out += parse_habr(response.content, remote=bool(f.get("remote_only")))
        return [v for v in out if not v.published or v.published >= since]

    async def fetch(self, source: str, f: dict, since: datetime) -> list[Vacancy]:
        return await {"hh": self.hh, "trudvsem": self.trudvsem, "habr": self.habr}[source](f, since)


def parse_hh_item(item: dict) -> Vacancy:
    salary = item.get("salary") or item.get("salary_range") or {}
    schedule = (item.get("schedule") or {}).get("id", "")
    formats = {(w or {}).get("id", "") for w in item.get("work_format") or []}
    employer = item.get("employer") or {}
    snippet = item.get("snippet") or {}
    return Vacancy(
        source="hh", ext_id=str(item["id"]), title=item.get("name") or "",
        company=employer.get("name") or "", city=(item.get("area") or {}).get("name") or "",
        salary_from=_int(salary.get("from")), salary_to=_int(salary.get("to")),
        currency=salary.get("currency") or "RUR",
        remote=schedule == "remote" or "REMOTE" in formats,
        experience=(item.get("experience") or {}).get("id") or "",
        url=item.get("alternate_url") or "",
        snippet=strip_tags(" ".join(filter(None, [snippet.get("requirement"), snippet.get("responsibility")]))),
        published=parse_dt(item.get("published_at")),
        employer={"id": str(employer["id"]) if employer.get("id") else None,
                  "url": employer.get("alternate_url") or None},
    )


def parse_trudvsem(data: dict) -> list[Vacancy]:
    out = []
    for wrap in ((data.get("results") or {}).get("vacancies") or []):
        v = wrap.get("vacancy") or {}
        schedule = norm(v.get("schedule"))
        company = v.get("company") or {}
        out.append(Vacancy(
            source="trudvsem", ext_id=str(v.get("id")), title=v.get("job-name") or "",
            company=company.get("name") or "", city=(v.get("region") or {}).get("name") or "",
            salary_from=_int(v.get("salary_min")), salary_to=_int(v.get("salary_max")),
            remote="удал" in schedule or "дистанц" in schedule,
            url=v.get("vac_url") or "", snippet=strip_tags(v.get("duty"))[:400],
            published=parse_dt(v.get("creation-date")),
            employer={"inn": company.get("inn") or None, "site": company.get("site") or company.get("url") or None},
        ))
    return out


def parse_habr(content: bytes, *, remote: bool = False) -> list[Vacancy]:
    out = []
    root = ET.fromstring(content)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        title = strip_tags(item.findtext("title"))
        number = re.search(r"/vacancies/(\d+)", link)
        company = ""
        tail = re.search(r"\(([^()]+)\)\s*$", title)          # «Требуется «Должность» (Компания)»
        if tail:
            company, title = tail.group(1).strip(), title[: tail.start()].strip()
        title = re.sub(r"^Требуется\s*", "", title).strip("«»\" ")
        out.append(Vacancy(source="habr", ext_id=number.group(1) if number else link[-160:], title=title,
                           company=company, remote=remote, url=link,
                           snippet=strip_tags(item.findtext("description"))[:400],
                           published=parse_rfc822(item.findtext("pubDate"))))
    return out
