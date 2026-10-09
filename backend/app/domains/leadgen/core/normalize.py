"""Нормализация ключей для склейки компаний (спецификация: «Дедупликация и склейка»)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

DEFAULT_PLATFORM_DOMAINS = (
    "avito.ru", "yandex.ru", "ya.ru", "2gis.ru", "vk.com", "vk.ru", "ok.ru",
    "profi.ru", "youdo.com", "ozon.ru", "wildberries.ru", "market.yandex.ru",
    "hh.ru", "otzovik.com", "flamp.ru", "zoon.ru", "irecommend.ru",
)

_LEGAL_FORMS = (
    "общество с ограниченной ответственностью",
    "индивидуальный предприниматель",
    "публичное акционерное общество",
    "акционерное общество",
    "ооо", "оао", "зао", "пао", "нао", "ао", "ип",
)
_QUOTES = "«»\"'„“”‘’`"


def normalize_domain(value: str | None) -> str | None:
    """'https://WWW.Кухни-Север.рф/contacts?x=1' -> 'xn--...xn--p1ai'. Хост целиком, без www и порта."""
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    host = urlsplit(raw).hostname
    if not host:
        return None
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in host:
        return None
    return host


def is_platform_domain(domain: str | None, platforms=DEFAULT_PLATFORM_DOMAINS) -> bool:
    if not domain:
        return False
    return any(domain == p or domain.endswith("." + p) for p in platforms)


def normalize_phone(value: str | None) -> str | None:
    """Российский номер -> '+7XXXXXXXXXX'. Иначе None."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return "+7" + digits


_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _check(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights)) % 11 % 10


def validate_inn(value: str | None) -> str | None:
    """Возвращает ИНН из 10/12 цифр, если контрольная сумма сходится, иначе None."""
    if not value:
        return None
    s = re.sub(r"\D", "", str(value))
    if len(s) not in (10, 12) or s == "0" * len(s):
        return None
    d = [int(c) for c in s]
    if len(s) == 10:
        return s if _check(d, _W10) == d[9] else None
    if _check(d, _W11) == d[10] and _check(d, _W12) == d[11]:
        return s
    return None


def clean_company_name(value: str | None) -> str | None:
    """'ООО «Кухни  Северная»' -> 'Кухни Северная' (для показа в письмах)."""
    if not value:
        return None
    s = value
    for q in _QUOTES:
        s = s.replace(q, " ")
    s = re.sub(r"\s+", " ", s).strip()
    low = s.lower()
    changed = True
    while changed:
        changed = False
        for form in _LEGAL_FORMS:
            if low.startswith(form + " "):
                s, low = s[len(form) + 1:].strip(), low[len(form) + 1:].strip()
                changed = True
            elif low.endswith(" " + form):
                s, low = s[: -len(form) - 1].strip(), low[: -len(form) - 1].strip()
                changed = True
    return s or None


def name_key(value: str | None, city: str | None = None) -> str | None:
    """Слабый ключ «название + город»: только для подсказок, никогда для автосклейки."""
    name = clean_company_name(value)
    if not name:
        return None
    key = re.sub(r"[^0-9a-zа-яё]+", " ", name.lower().replace("ё", "е")).strip()
    if city:
        key += "|" + re.sub(r"\s+", " ", city.lower().replace("ё", "е")).strip()
    return key or None


def text_key(value: str | None) -> str | None:
    """Ключ для сравнения подписей (город, ниша) без учёта регистра и ё — одинаково в SQLite и Postgres."""
    if not value:
        return None
    key = re.sub(r"\s+", " ", value.replace("ё", "е").replace("Ё", "Е")).strip().lower()
    return key or None


def display_domain(domain: str | None) -> str | None:
    """xn--...рф -> добрые-кухни.рф: кириллические домены показываем по-русски."""
    if not domain or "xn--" not in domain:
        return domain
    try:
        return domain.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return domain
