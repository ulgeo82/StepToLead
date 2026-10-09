"""Провайдеры выдачи для «Поиска компаний».

XMLStock Live отдаёт живую выдачу Яндекса вместе с рекламой Директа (обычный Yandex XML рекламу не отдаёт).
Реклама приходит только с параметром ads=1: блоки <topads> (спецразмещение) и <bottomads>,
в каждом <query> с <url>, <title>, <snippet>. Формат сверен по реальному ответу 09.10.2026.

Ключи берутся только из окружения: XMLSTOCK_USER, XMLSTOCK_KEY, XMLSTOCK_LIVE_URL (.env не меняется кодом).
"""
from __future__ import annotations

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from dataclasses import dataclass, field

from app.domains.leadgen.core.serp import SerpAd, SerpOrganic

DEVICES = ("desktop", "mobile")


@dataclass
class SerpPage:
    ads: list[SerpAd] = field(default_factory=list)
    organic: list[SerpOrganic] = field(default_factory=list)
    has_ad_blocks: bool = True


class ProviderNotConfigured(RuntimeError):
    pass


class FormatNotVerified(RuntimeError):
    pass


class ProviderError(RuntimeError):
    """Ошибка от XMLStock (нет денег, неверный ключ, лимит) — текст показываем как есть."""


class NoAdBlocks(ProviderError):
    """Живая выдача пришла без блоков рекламы: бывает при сбоях — стоит повторить запрос."""


# Коды XMLStock, при которых помогает пауза и повтор (перегрузка / временная ошибка).
RETRY_CODES = {"20", "110"}
FATAL_MARKERS = ("средств", "ключ", "key", "user", "заблокирован", "доступ")


def _text(node, tag: str) -> str | None:
    el = node.find(tag)
    value = (el.text or "").strip() if el is not None else ""
    return " ".join(value.split()) or None


def parse_xmlstock_live(raw: str, keyword: str) -> list[SerpAd]:
    """Ответ XMLStock Live -> объявления Директа (верхний блок = спецразмещение)."""
    page = parse_xmlstock_page(raw, keyword)
    if not page.has_ad_blocks:
        raise NoAdBlocks("в ответе нет рекламных блоков")
    return page.ads


def _all_text(node) -> str | None:
    if node is None:
        return None
    value = " ".join("".join(node.itertext()).split())
    return value or None


def parse_xmlstock_page(raw: str, keyword: str) -> SerpPage:
    """Ответ XMLStock Live -> реклама (topads/bottomads) и органика (results/grouping/group/doc)."""
    try:
        root = ET.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except ET.ParseError as exc:
        raise ProviderError(f"XMLStock вернул не XML: {raw[:200]!r}") from exc
    error = root.find(".//error")
    if error is not None:
        code = error.get("code") or ""
        raise ProviderError(f"XMLStock: {(error.text or '').strip() or 'ошибка'}{f' (код {code})' if code else ''}")
    response = root.find("response")
    if response is None:
        raise ProviderError("XMLStock: в ответе нет блока response")
    ads: list[SerpAd] = []
    has_blocks = response.find("topads") is not None or response.find("bottomads") is not None
    for block, premium in (("topads", True), ("bottomads", False)):
        node = response.find(block)
        if node is None:
            continue
        for position, item in enumerate(node.findall("query"), start=1):
            url = _text(item, "url")
            if not url:
                continue
            ads.append(SerpAd(keyword=keyword, url=url, title=_text(item, "title"), text=_text(item, "snippet"),
                              position=position, premium=premium))
    organic: list[SerpOrganic] = []
    for position, doc in enumerate(response.findall("results/grouping/group/doc"), start=1):
        url = _text(doc, "url")
        if not url or "yabs.yandex" in url:
            continue  # реклама, встроенная в органику
        organic.append(SerpOrganic(keyword=keyword, url=url, title=_all_text(doc.find("title")),
                                   text=_all_text(doc.find("passages")), position=position))
    return SerpPage(ads, organic, has_blocks)


class XmlStockLiveProvider:
    name = "xmlstock_live"

    def __init__(self, *, user: str | None = None, key: str | None = None, url: str | None = None,
                 cost_per_1000_rub: float = 25.0, timeout: float = 40.0, extra_params: dict | None = None):
        self.user = user or os.getenv("XMLSTOCK_USER", "")
        self.key = key or os.getenv("XMLSTOCK_KEY", "")
        self.url = url or os.getenv("XMLSTOCK_LIVE_URL", "")
        self.cost_per_request_rub = cost_per_1000_rub / 1000
        self.timeout = timeout
        self.extra_params = extra_params or {}
        if not (self.user and self.key and self.url):
            raise ProviderNotConfigured("XMLSTOCK_USER, XMLSTOCK_KEY и XMLSTOCK_LIVE_URL не заданы")

    async def fetch_raw(self, keyword: str, region_code: int | None, device: str = "desktop") -> str:
        # ads=1 обязателен: без него XMLStock Live не отдаёт рекламные блоки.
        params = {"user": self.user, "key": self.key, "query": keyword, "ads": 1, "device": device,
                  **self.extra_params}
        if region_code:
            params["lr"] = region_code
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self.url, params=params)
            resp.raise_for_status()
            return resp.text

    retries = 3            # всего попыток на ключ
    retry_pause = 10.0     # пауза перед повтором (рекомендация XMLStock для кодов 20/110)
    min_interval = 1.0     # не чаще 1 запроса в секунду
    _last_at = 0.0

    async def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_at = time.monotonic()

    async def search(self, keyword: str, region_code: int | None) -> list[SerpAd]:
        return (await self.search_page(keyword, region_code)).ads

    async def search_page(self, keyword: str, region_code: int | None, device: str = "desktop") -> SerpPage:
        last: Exception | None = None
        page: SerpPage | None = None
        for attempt in range(self.retries):
            if attempt:
                await asyncio.sleep(self.retry_pause)
            await self._throttle()
            try:
                page = parse_xmlstock_page(await self.fetch_raw(keyword, region_code, device), keyword)
                # Без рекламных блоков повторяем один раз; если и во второй раз нет — значит, её правда нет.
                if page.has_ad_blocks or attempt >= 1:
                    return page
                last = None
            except ProviderError as exc:
                text = str(exc).lower()
                if any(m in text for m in FATAL_MARKERS) and not any(f"код {c}" in text for c in RETRY_CODES):
                    raise  # нет денег / неверный ключ — повторять бессмысленно
                last = exc
            except httpx.HTTPError as exc:
                last = exc
        if page is not None:
            return page  # повтор сорвался, но первый ответ (без рекламы, с органикой) уже есть
        if last is None:
            return SerpPage(has_ad_blocks=False)
        raise last

    async def save_sample(self, keyword: str, region_code: int | None, path: str | Path) -> Path:
        """Один реальный запрос -> файл. Ключи в файл не пишутся."""
        raw = await self.fetch_raw(keyword, region_code)
        out = Path(path)
        out.write_text(raw, encoding="utf-8")
        return out


class StaticProvider:
    """Подставной провайдер: тесты и демо без сети."""
    name = "static"

    def __init__(self, by_keyword: dict[str, list[SerpAd]] | None = None, *, cost_per_request_rub: float = 0.012,
                 failing: set[str] | None = None, organic: dict[str, list[SerpOrganic]] | None = None,
                 mobile: dict[str, list[SerpAd]] | None = None):
        self.by_keyword = {k.lower(): v for k, v in (by_keyword or {}).items()}
        self.organic = {k.lower(): v for k, v in (organic or {}).items()}
        self.mobile = {k.lower(): v for k, v in (mobile or {}).items()}
        self.cost_per_request_rub = cost_per_request_rub
        self.failing = {k.lower() for k in (failing or set())}
        self.calls: list[tuple[str, int | None]] = []
        self.devices: list[str] = []

    async def search_page(self, keyword: str, region_code: int | None, device: str = "desktop") -> SerpPage:
        self.devices.append(device)
        source = self.mobile if device == "mobile" else None
        ads = ([SerpAd(**{**ad.__dict__, "keyword": keyword}) for ad in source.get(keyword.lower(), [])]
               if source is not None else await self.search(keyword, region_code))
        organic = [SerpOrganic(**{**o.__dict__, "keyword": keyword}) for o in self.organic.get(keyword.lower(), [])]
        return SerpPage(ads, organic)

    async def search(self, keyword: str, region_code: int | None) -> list[SerpAd]:
        self.calls.append((keyword, region_code))
        if keyword.lower() in self.failing:
            raise RuntimeError("provider error")
        return [SerpAd(**{**ad.__dict__, "keyword": keyword}) for ad in self.by_keyword.get(keyword.lower(), [])]
