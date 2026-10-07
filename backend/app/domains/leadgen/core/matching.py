"""Решение «к какой компании прикрепить находку» (спецификация: «Дедупликация и склейка»).

Чистая логика: сервис портала передаёт lookup(kind, value_norm) -> список Candidate из lg_company_key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .normalize import (
    is_platform_domain, name_key, normalize_domain, normalize_phone, validate_inn,
)


@dataclass
class Finding:
    inn: str | None = None
    domain: str | None = None
    phones: list[str] = field(default_factory=list)
    city: str | None = None
    twogis_id: str | None = None
    hh_employer_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Candidate:
    company_id: str
    city: str | None = None
    inn: str | None = None


@dataclass
class MatchResult:
    action: str                       # attach / create / review / skip
    company_id: str | None = None
    matched_by: str | None = None
    reason: str | None = None
    suggestions: list[str] = field(default_factory=list)  # кандидаты по name_city
    keys: dict[str, list[str]] = field(default_factory=dict)  # нормализованные ключи находки


Lookup = Callable[[str, str], list[Candidate]]


def _same_city(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().lower().replace("ё", "е") == b.strip().lower().replace("ё", "е")


def normalized_keys(f: Finding, platforms=None) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    inn = validate_inn(f.inn)
    if inn:
        keys["inn"] = [inn]
    dom = normalize_domain(f.domain)
    if dom:
        keys["domain"] = [dom]
    phones = sorted({p for p in (normalize_phone(x) for x in f.phones) if p})
    if phones:
        keys["phone"] = phones
    if f.twogis_id:
        keys["twogis_id"] = [str(f.twogis_id)]
    if f.hh_employer_id:
        keys["hh_employer_id"] = [str(f.hh_employer_id)]
    nk = name_key(f.name, f.city)
    if nk:
        keys["name_city"] = [nk]
    return keys


def plan_match(f: Finding, lookup: Lookup, platforms=None) -> MatchResult:
    """Порядок: ИНН → домен → ID 2ГИС/HH → телефон+город. Название+город — только подсказка."""
    dom = normalize_domain(f.domain)
    if dom and (is_platform_domain(dom, platforms) if platforms else is_platform_domain(dom)):
        return MatchResult("skip", reason="platform_domain")

    keys = normalized_keys(f, platforms)
    inn = keys.get("inn", [None])[0]

    def done(r: MatchResult) -> MatchResult:
        r.keys = keys
        return r

    if inn:
        hits = lookup("inn", inn)
        if hits:
            return done(MatchResult("attach", hits[0].company_id, "inn"))

    if dom:
        hits = lookup("domain", dom)
        if hits:
            c = hits[0]
            if inn and c.inn and c.inn != inn:
                return done(MatchResult("review", c.company_id, "domain", reason="domain_inn_conflict"))
            return done(MatchResult("attach", c.company_id, "domain"))

    for kind in ("twogis_id", "hh_employer_id"):
        for v in keys.get(kind, []):
            hits = lookup(kind, v)
            if hits:
                return done(MatchResult("attach", hits[0].company_id, kind))

    for phone in keys.get("phone", []):
        hits = lookup("phone", phone)
        if not hits:
            continue
        same = [c for c in hits if _same_city(c.city, f.city)]
        if same:
            c = same[0]
            if inn and c.inn and c.inn != inn:
                return done(MatchResult("review", c.company_id, "phone", reason="phone_inn_conflict"))
            return done(MatchResult("attach", c.company_id, "phone"))
        return done(MatchResult("review", hits[0].company_id, "phone", reason="phone_other_city"))

    suggestions = []
    for v in keys.get("name_city", []):
        suggestions = [c.company_id for c in lookup("name_city", v)]
    return done(MatchResult("create", suggestions=suggestions))
