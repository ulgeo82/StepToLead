"""Фильтр вакансий и склейка одной вакансии с разных сайтов. Чистые функции — без базы и сети."""
from __future__ import annotations

import re
from collections import defaultdict

from app.domains.jobradar.sources import Vacancy, fingerprint, norm
from app.domains.jobradar.sources.regions import codes_for

PRIORITY = {"hh": 0, "superjob": 1, "getmatch": 2, "habr": 3, "trudvsem": 4, "telegram": 5, "flru": 6}   # главная в посте
RUB = ("RUR", "RUB", "", None)


def text(value: str | None) -> str:
    """Для сравнения: без регистра и «ё/е», дефисы и лишние пробелы — один пробел («Руководитель  отдела»)."""
    return re.sub(r"[\s\-–—/]+", " ", norm(value)).strip()


def has_keyword(title: str, keyword: str) -> bool:
    """Фраза целиком — или все её значимые слова (длиннее 2 букв) в названии в любом месте:
    «менеджер по маркетингу» находит «Менеджер (в подразделениях) по маркетингу»."""
    phrase = text(keyword)
    if not phrase:
        return False
    if phrase in title:
        return True
    words = [w for w in phrase.split() if len(w) > 2]
    return len(words) > 1 and all(w in title for w in words)


def city_ok(v: Vacancy, regions: list[str]) -> bool:
    """Город из фильтра в названии города вакансии — или тот же регион по коду («Самара» = «Самарская область»)."""
    place = text(v.city)
    if any(text(r) and text(r) in place for r in regions):
        return True
    codes, _ = codes_for(regions)
    return bool(v.region_code and v.region_code in codes)


def matches(v: Vacancy, f: dict) -> tuple[bool, str]:
    """(подходит, почему нет). f — фильтр как словарь (см. runner.as_dict)."""
    title = text(v.title)
    if not any(has_keyword(title, k) for k in f.get("keywords") or []):
        return False, "нет ключевого слова в названии"
    for word in f.get("exclude") or []:
        if text(word) and text(word) in title:
            return False, f"исключение «{word}»"
    if f.get("remote_only") and not v.remote:
        return False, "не удалёнка"
    regions = [r for r in f.get("regions") or [] if text(r)]
    # Вакансия на удалёнке подходит из любого города; для лидов важен город самой компании (strict_city).
    if regions and (f.get("strict_city") or not v.remote) and not city_ok(v, regions):
        return False, "не тот город"
    if not getattr(v, "salary_known", True):
        return True, ""   # площадка не отдаёт зарплату и опыт (Хабр RSS) — эти условия к ней не применяем
    floor = int(f.get("salary_from") or 0)
    top = v.salary_to or v.salary_from
    if top is None:
        if not f.get("allow_no_salary", True):
            return False, "без зарплаты"
    elif floor and v.currency in RUB and top < floor:
        return False, "зарплата ниже порога"
    wanted = f.get("experience") or []
    if wanted and v.experience and v.experience not in wanted:
        return False, "опыт не подходит"
    return True, ""


def group(vacancies: list[Vacancy]) -> dict[str, list[Vacancy]]:
    """fingerprint -> карточки с разных сайтов, главная первой (hh, потом Хабр, потом «Работа России»)."""
    groups: dict[str, list[Vacancy]] = defaultdict(list)
    seen: set[str] = set()
    for v in vacancies:
        if v.uid in seen:
            continue
        seen.add(v.uid)
        groups[fingerprint(v.title, v.company)].append(v)
    return {fp: sorted(items, key=lambda v: PRIORITY.get(v.source, 9)) for fp, items in groups.items()}
