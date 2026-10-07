"""Провайдеры выдачи для «Поиска компаний».

XMLStock Live отдаёт живую выдачу Яндекса вместе с рекламой Директа (обычный Yandex XML рекламу не отдаёт).
Точный формат ответа ещё не сверен: parse_xmlstock_live() включится после первого реального ответа.
До этого save_sample() сохраняет сырой ответ, по нему пишется и тестируется разбор.

Ключи берутся только из окружения: XMLSTOCK_USER, XMLSTOCK_KEY, XMLSTOCK_LIVE_URL (.env не меняется кодом).
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from app.domains.leadgen.core.serp import SerpAd


class ProviderNotConfigured(RuntimeError):
    pass


class FormatNotVerified(RuntimeError):
    pass


def parse_xmlstock_live(raw: str, keyword: str) -> list[SerpAd]:
    """Разбор ответа XMLStock Live в SerpAd. Пишется по реальному образцу ответа."""
    raise FormatNotVerified(
        "Формат ответа XMLStock Live не сверен. Сохраните образец через save_sample() и допишите разбор.")


class XmlStockLiveProvider:
    name = "xmlstock_live"

    def __init__(self, *, user: str | None = None, key: str | None = None, url: str | None = None,
                 cost_per_1000_rub: float = 12.0, timeout: float = 40.0, extra_params: dict | None = None):
        self.user = user or os.getenv("XMLSTOCK_USER", "")
        self.key = key or os.getenv("XMLSTOCK_KEY", "")
        self.url = url or os.getenv("XMLSTOCK_LIVE_URL", "")
        self.cost_per_request_rub = cost_per_1000_rub / 1000
        self.timeout = timeout
        self.extra_params = extra_params or {}
        if not (self.user and self.key and self.url):
            raise ProviderNotConfigured("XMLSTOCK_USER, XMLSTOCK_KEY и XMLSTOCK_LIVE_URL не заданы")

    async def fetch_raw(self, keyword: str, region_code: int | None) -> str:
        params = {"user": self.user, "key": self.key, "query": keyword, **self.extra_params}
        if region_code:
            params["lr"] = region_code
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self.url, params=params)
            resp.raise_for_status()
            return resp.text

    async def search(self, keyword: str, region_code: int | None) -> list[SerpAd]:
        return parse_xmlstock_live(await self.fetch_raw(keyword, region_code), keyword)

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
