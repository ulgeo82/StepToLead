"""Фильтр вакансий и склейка одной вакансии с разных сайтов. Чистые функции — без базы и сети."""
from __future__ import annotations

from collections import defaultdict

from app.domains.jobradar.sources import Vacancy, fingerprint, norm

PRIORITY = {"hh": 0, "superjob": 1, "getmatch": 2, "habr": 3, "trudvsem": 4, "telegram": 5, "flru": 6}   # главная в посте
RUB = ("RUR", "RUB", "", None)


def matches(v: Vacancy, f: dict) -> tuple[bool, str]:
    """(подходит, почему нет). f — фильтр как словарь (см. runner.as_dict)."""
    title = norm(v.title)
    if not any(norm(k).strip() and norm(k).strip() in title for k in f.get("keywords") or []):
        return False, "нет ключевого слова в названии"
    for word in f.get("exclude") or []:
        if norm(word).strip() and norm(word).strip() in title:
            return False, f"исключение «{word}»"
    if f.get("remote_only") and not v.remote:
        return False, "не удалёнка"
    regions = [norm(r).strip() for r in f.get("regions") or [] if norm(r).strip()]
    # Вакансия на удалёнке подходит из любого города; для лидов важен город самой компании (strict_city).
    if regions and (f.get("strict_city") or not v.remote) and not any(r in norm(v.city) for r in regions):
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
