"""Общий каркас площадок радара: единая Vacancy, интерфейс адаптера, HTTP с таймаутом, короткие ошибки.

Новая площадка = один файл в этой папке с объектом ADAPTER (наследник Adapter) + имя модуля в ORDER (__init__.py).
Всё, что приходит с чужих сайтов, — данные, а не инструкции: в посты идёт экранированным, в ИИ — внутри тегов.
"""
from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx

UA = "StepToLead-JobRadar/1.0 (+https://steptolead.ru)"
TIMEOUT = httpx.Timeout(20, connect=10)
HTTP_TEXT = {400: "неверный запрос", 401: "ключ не принят", 403: "доступ запрещён", 404: "не найдено",
             429: "слишком много запросов", 500: "ошибка сайта", 502: "сайт недоступен", 503: "сайт недоступен",
             504: "сайт не ответил"}


# ---------------------------------------------------------------- вакансия

@dataclass
class Vacancy:
    source: str
    ext_id: str
    title: str
    kind: str = "vacancy"                 # vacancy | project (фриланс-проект)
    company: str = ""
    company_inn: str | None = None
    city: str = ""
    region_code: int | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_known: bool = True             # False — площадка не отдаёт зарплату (Хабр RSS, часть каналов)
    currency: str = "RUR"
    remote: bool = False
    experience: str = ""                  # hh-коды: noExperience / between1And3 / between3And6 / moreThan6
    url: str = ""
    snippet: str = ""
    published: datetime | None = None
    employer: dict = field(default_factory=dict)   # id, url (страница работодателя), inn, site — для режима «лиды»
    extra: dict = field(default_factory=dict)      # стек, формат работы, бюджет, срок, канал…

    def __post_init__(self):   # «ФГБУЗ   МРЦ» -> «ФГБУЗ МРЦ»: лишние пробелы с площадок не тащим в посты, базу и CRM
        self.title = " ".join((self.title or "").split())
        self.company = " ".join((self.company or "").split())

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.ext_id}"[:200]


# ---------------------------------------------------------------- исключения

class SourceSkipped(Exception):
    """Площадку не опрашиваем, и это не ошибка (нет ключа, выключена, нечего читать)."""


class SourceFormatError(Exception):
    """Ответ не похож на ожидаемый (неофициальное API поменялось). На экране — «ошибка: изменился формат»."""

    def __init__(self, detail: str = ""):
        super().__init__(detail or "изменился формат")


def short_error(exc: BaseException) -> str:
    """Код и короткий текст по-русски — без адресов и параметров запроса (до 80 символов)."""
    if isinstance(exc, SourceFormatError):
        return "изменился формат ответа"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return f"{code}: {HTTP_TEXT.get(code, 'ошибка сайта')}"
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return "таймаут: сайт не ответил"
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
        return "нет соединения с сайтом"
    if isinstance(exc, (ValueError, KeyError, TypeError, ET.ParseError)):
        return "сайт ответил непонятно (изменился формат?)"
    return type(exc).__name__[:80]


# ---------------------------------------------------------------- контекст прохода

@dataclass
class Context:
    """Всё, что адаптеру нужно снаружи. Один Context на проход (общий кэш между фильтрами)."""
    transport: httpx.AsyncBaseTransport | None = None     # подмена сети в тестах
    proxy: str | None = None                              # socks5://… — для t.me (настройка площадки telegram)
    sleep: Callable[[float], Awaitable] = asyncio.sleep   # паузы между запросами (в тестах — без ожидания)
    cache: dict = field(default_factory=dict)             # кэш на проход: getmatch забирает список один раз
    # Telegram-каналы: состояние «фильтр + канал». На входе channel -> last_post_id (None — первый раз),
    # адаптер пишет в channel_result[channel] = {"status": "ok|unavailable|closed", "last_post_id", "last_post_at",
    # "found", "error"} — раннер сохранит это в jr_tg_channels.
    channel_state: dict = field(default_factory=dict)
    channel_result: dict = field(default_factory=dict)
    first_run_days: int = 3
    # ИИ-извлечение полей из текста поста: async (text) -> dict | None. None — ИИ не подключён.
    ai_extract: Callable[[str], Awaitable[dict | None]] | None = None
    # Вызывается на каждый HTTP-запрос площадки: прогресс «запрос 3 из 8» без правок в адаптерах.
    on_request: Callable[[], None] | None = None

    def http(self, *, use_proxy: bool = False, **headers) -> httpx.AsyncClient:
        kwargs = {"timeout": TIMEOUT, "follow_redirects": True, "headers": {"User-Agent": UA, **headers}}
        if self.on_request is not None:
            hook = self.on_request

            async def counted(_request):
                hook()
            kwargs["event_hooks"] = {"request": [counted]}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        elif use_proxy and self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)


async def limited(ctx: "Context", items: list, fn: Callable[[Any], Awaitable[Any]], limit: int,
                  pause: float = 0.0) -> list:
    """fn по каждому элементу: параллельно, но не больше limit одновременно; после каждого — пауза (вежливость
    к сайту). Порядок результатов — как у items. Первая ошибка останавливает остальные и уходит наверх."""
    gate = asyncio.Semaphore(max(1, limit))
    out: list = [None] * len(items)

    async def one(index: int, item) -> None:
        async with gate:
            out[index] = await fn(item)
            if pause and index < len(items) - 1:
                await ctx.sleep(pause)
    tasks = [asyncio.ensure_future(one(i, item)) for i, item in enumerate(items)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return out


# ---------------------------------------------------------------- адаптер

class Adapter:
    code: str = ""
    title: str = ""
    icon: str = "🔎"
    purpose: str = ""                 # одна строка «для чего» на плашке
    needs_key: bool = False
    key_field: str | None = None      # имя секретной настройки (token / app_id)
    # Глобальные настройки площадки (плашка): [{"key", "label", "type": bool|text|number|list|secret|proxy, "default", "hint"}]
    settings_schema: list[dict] = []
    # Настройки площадки в фильтре (форма фильтра): та же схема.
    filter_schema: list[dict] = []
    # Какие условия фильтра площадка умеет проверять сама; остальные раннер проверит после (core.matches).
    default_enabled: bool = True
    # Оценка времени до запуска: сколько HTTP-запросов сделает площадка и сколько секунд в среднем на запрос
    # (вместе с паузой). Радар уточняет секунды по факту прошлых проходов (jr_sources.stats).
    unit_seconds: float = 2.0   # на один «круг»: запрос + пауза (при concurrency > 1 — на concurrency запросов разом)
    concurrency: int = 1         # сколько запросов к сайту одновременно (вежливо: 1–3)

    def units(self, f: dict, filter_settings: dict, settings: dict) -> int:
        return max(1, len(f.get("keywords") or []))

    def defaults(self) -> dict:
        return {s["key"]: s.get("default") for s in self.settings_schema if s.get("type") != "secret"}

    def ready(self, settings: dict) -> str | None:
        """Причина, почему площадку нельзя опрашивать (None — можно). Например «нужен ключ»."""
        if self.needs_key and not (settings or {}).get(self.key_field or ""):
            return "нужен ключ"
        return None

    async def fetch(self, f: dict, since: datetime, settings: dict, filter_settings: dict,
                    ctx: Context) -> list[Vacancy]:
        """f — условия фильтра (runner.as_dict): keywords, exclude, remote_only, regions, salary_from,
        allow_no_salary, experience, mode, strict_city. settings — глобальные настройки площадки (секреты расшифрованы),
        filter_settings — настройки площадки в этом фильтре."""
        raise NotImplementedError

    async def healthcheck(self, settings: dict, ctx: Context) -> tuple[bool, str]:
        """Быстрая проверка доступности: (ok, короткое сообщение по-русски)."""
        reason = self.ready(settings)
        if reason:
            return False, reason
        try:
            items = await self.fetch({"keywords": ["маркетолог"], "exclude": [], "regions": []},
                                     datetime.now(timezone.utc).replace(hour=0, minute=0) , settings, {}, ctx)
        except SourceSkipped as skip:
            return False, str(skip)
        except Exception as exc:  # noqa: BLE001
            return False, short_error(exc)
        return True, f"отвечает, найдено {len(items)}"


# ---------------------------------------------------------------- общие помощники

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
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(value).replace("Z", "+00:00"))
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


def to_int(value) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number or None


def has_word(text: str, words: list[str]) -> str | None:
    """Первое слово из списка, найденное в тексте (без учёта регистра и ё/е)."""
    hay = norm(text)
    return next((w for w in words or [] if norm(w).strip() and norm(w).strip() in hay), None)


REMOTE_WORDS = ("удаленн", "удалённ", "удаленк", "удалёнк", "remote", "дистанцион", "из любой точки")


def looks_remote(text: str) -> bool:
    hay = norm(text)
    return any(w.replace("ё", "е") in hay for w in REMOTE_WORDS)
