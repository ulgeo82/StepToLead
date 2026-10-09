"""Выдача с рекламой -> рекламодатели. Чистая логика: без сети и БД.

Провайдер (XMLStock Live и т.п.) отдаёт по каждому ключу список SerpAd; здесь они склеиваются по домену.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .normalize import DEFAULT_PLATFORM_DOMAINS, is_platform_domain, normalize_domain

DIRECT_MIN_KEYWORDS = 3  # сигнал ad_direct: показы по 3+ ключам


@dataclass
class SerpAd:
    keyword: str
    url: str                      # посадочная или ссылка объявления
    title: str | None = None
    text: str | None = None
    display_url: str | None = None
    sitelinks: list[str] = field(default_factory=list)
    position: int | None = None
    premium: bool = False         # спецразмещение (верхний блок)


@dataclass
class AdView:
    content_hash: str
    title: str | None
    text: str | None
    display_url: str | None
    landing_url: str | None
    sitelinks: list[str]
    keywords: set[str]
    premium: bool


@dataclass
class Advertiser:
    domain: str
    keywords: set[str] = field(default_factory=set)
    premium: bool = False
    ads: dict[str, AdView] = field(default_factory=dict)
    landing_url: str | None = None

    @property
    def keyword_count(self) -> int:
        return len(self.keywords)


def ad_hash(title: str | None, text: str | None) -> str:
    raw = f"{(title or '').strip().lower()}\n{(text or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def aggregate(ads: list[SerpAd], platforms=DEFAULT_PLATFORM_DOMAINS) -> dict[str, Advertiser]:
    """Склеивает объявления по домену. Площадки и мусорные ссылки отбрасываются."""
    result: dict[str, Advertiser] = {}
    for ad in ads:
        domain = normalize_domain(ad.display_url) if ad.display_url else None
        domain = domain or normalize_domain(ad.url)
        if not domain or is_platform_domain(domain, platforms):
            continue
        adv = result.setdefault(domain, Advertiser(domain=domain))
        kw = ad.keyword.strip().lower()
        adv.keywords.add(kw)
        adv.premium = adv.premium or ad.premium
        adv.landing_url = adv.landing_url or ad.url
        h = ad_hash(ad.title, ad.text)
        view = adv.ads.get(h)
        if view is None:
            adv.ads[h] = AdView(h, ad.title, ad.text, ad.display_url, ad.url, list(ad.sitelinks), {kw}, ad.premium)
        else:
            view.keywords.add(kw)
            view.premium = view.premium or ad.premium
    return result


@dataclass
class SerpOrganic:
    keyword: str
    url: str
    title: str | None = None
    text: str | None = None
    position: int | None = None


@dataclass
class OrganicHit:
    domain: str
    url: str
    title: str | None
    keywords: set[str] = field(default_factory=set)
    best_position: int | None = None


def aggregate_organic(items: list[SerpOrganic], platforms=DEFAULT_PLATFORM_DOMAINS) -> dict[str, OrganicHit]:
    """Органика по доменам: агрегаторы, маркетплейсы и ссылки Яндекса отбрасываются."""
    result: dict[str, OrganicHit] = {}
    for item in items:
        domain = normalize_domain(item.url)
        if not domain or is_platform_domain(domain, platforms):
            continue
        hit = result.setdefault(domain, OrganicHit(domain, item.url, item.title))
        hit.keywords.add(item.keyword.strip().lower())
        if item.position and (hit.best_position is None or item.position < hit.best_position):
            hit.best_position, hit.url, hit.title = item.position, item.url, item.title or hit.title
    return result


def run_fingerprint(source: str, region_code: int | None, keywords: list[str]) -> str:
    """Одинаковые запуски (источник + регион + набор ключей) сравниваются между собой."""
    kws = "|".join(sorted({k.strip().lower() for k in keywords if k.strip()}))
    return hashlib.sha256(f"{source}#{region_code}#{kws}".encode("utf-8")).hexdigest()[:32]
