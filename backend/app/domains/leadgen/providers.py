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

from app.domains.leadgen.core.serp import SerpAd


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
    if response.find("topads") is None and response.find("bottomads") is None:
        raise NoAdBlocks("в ответе нет рекламных блоков")
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
    return ads


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

    async def fetch_raw(self, keyword: str, region_code: int | None) -> str:
        # ads=1 обязателен: без него XMLStock Live не отдаёт рекламные блоки.
        params = {"user": self.user, "key": self.key, "query": keyword, "ads": 1, **self.extra_params}
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
        last: Exception | None = None
        for attempt in range(self.retries):
            if attempt:
                await asyncio.sleep(self.retry_pause)
            await self._throttle()
            try:
                return parse_xmlstock_live(await self.fetch_raw(keyword, region_code), keyword)
            except NoAdBlocks:
                # Повторяем один раз; если и во второй раз рекламы нет — значит, её правда нет.
                if attempt >= 1:
                    return []
                last = None
            except ProviderError as exc:
                text = str(exc).lower()
                if any(m in text for m in FATAL_MARKERS) and not any(f"код {c}" in text for c in RETRY_CODES):
                    raise  # нет денег / неверный ключ — повторять бессмысленно
                last = exc
            except httpx.HTTPError as exc:
                last = exc
        if last is None:
            return []
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
                 failing: set[str] | None = None):
        self.by_keyword = {k.lower(): v for k, v in (by_keyword or {}).items()}
        self.cost_per_request_rub = cost_per_request_rub
        self.failing = {k.lower() for k in (failing or set())}
        self.calls: list[tuple[str, int | None]] = []

    async def search(self, keyword: str, region_code: int | None) -> list[SerpAd]:
        self.calls.append((keyword, region_code))
        if keyword.lower() in self.failing:
            raise RuntimeError("provider error")
        return [SerpAd(**{**ad.__dict__, "keyword": keyword}) for ad in self.by_keyword.get(keyword.lower(), [])]
