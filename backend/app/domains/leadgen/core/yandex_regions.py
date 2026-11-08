"""Город / регион -> код региона Яндекса (lr) для поисковой выдачи. Число в поле («51») берётся как есть.

Только проверенные коды. Нет в списке — экран просит ввести код числом (коды регионов Яндекса).
"""
from __future__ import annotations

CODES: dict[str, int] = {
    "москва": 213, "санкт-петербург": 2, "петербург": 2, "спб": 2,
    "самара": 51, "тольятти": 240, "казань": 43, "нижний новгород": 47, "уфа": 172, "екатеринбург": 54,
    "новосибирск": 65, "краснодар": 35, "ростов-на-дону": 39, "челябинск": 56, "пермь": 50, "воронеж": 193,
    "волгоград": 38, "красноярск": 62, "омск": 66, "саратов": 194,
}


def _key(text: str) -> str:
    key = (text or "").lower().replace("ё", "е").replace("г.", " ").strip()
    return " ".join(key.split())


def lr_for(place: str | int | None) -> int | None:
    if place is None:
        return None
    text = str(place).strip()
    if text.isdigit():
        return int(text) or None
    return CODES.get(_key(text))


def city_words() -> set[str]:
    """Названия городов — чтобы вычистить город из ИИ-подбора, когда регион не задан."""
    from app.domains.jobradar.sources.regions import CODES as RF
    return {k for k in list(CODES) + list(RF) if len(k) >= 3} | {"мск", "спб"}
