"""Тех-профиль сайта по html главной: аналитика, коллтрекинг, чат, CRM-формы, платформа, год в подвале, политика ПДн.
Чистая логика без сети — тестируется на сохранённых страницах."""
from __future__ import annotations

import re
from html import unescape

from .site_extract import CHAT_MARKERS, CRM_MARKERS, METRIKA_MARKERS, QUIZ_MARKERS

GA_MARKERS = ("googletagmanager.com/gtag/js", "google-analytics.com/analytics.js", "google-analytics.com/ga.js")
GTM_MARKERS = ("googletagmanager.com/gtm.js",)
CALLTRACKING_MARKERS = {
    "calltouch": ("calltouch.ru", "ct_site_id", "calltouch"),
    "comagic": ("comagic.ru", "comagic.widget", "__cs.push"),
    "roistat": ("roistat.com", "roistat_visit", "roistat"),
    "mango": ("widgets.mango-office.ru", "mango-office.ru/calltracking", "mango-office.ru/widgets", "mangocalltracking"),
    "callibri": ("callibri.ru", "callibri"),
}
EXTRA_CHATS = {"talk-me": ("talk-me.ru", "talkme"), "livetex": ("livetex",), "carrot": ("carrotquest",)}
PLATFORM_MARKERS = (   # порядок важен: первая совпавшая
    ("tilda", ("tildacdn.com", "tilda.ws", "data-tilda-", "tilda-blocks")),
    ("bitrix", ("/bitrix/js/", "/bitrix/templates/", "bx-core", "bitrix/cache")),
    ("wordpress", ("/wp-content/", "/wp-includes/")),
    ("wix", ("wixstatic.com", "x-wix-")),
    ("flexbe", ("flexbe.com", "flexbe.ru")),
    ("lpgenerator", ("lpgenerator",)),
    ("insales", ("insales.ru", "assets.insales")),
    ("nethouse", ("nethouse.ru",)),
    ("megagroup", ("megagroup.ru",)),
    ("joomla", ("/media/jui/", "joomla")),
    ("modx", ("modx",)),
    ("opencart", ("catalog/view/theme",)),
)
PLATFORM_TITLES = {"tilda": "Тильда", "bitrix": "1С-Битрикс", "wordpress": "WordPress", "wix": "Wix",
                   "flexbe": "Flexbe", "lpgenerator": "LPgenerator", "insales": "InSales", "nethouse": "Nethouse",
                   "megagroup": "Мегагрупп", "joomla": "Joomla", "modx": "MODX", "opencart": "OpenCart"}
TRACKING_TITLES = {"calltouch": "Calltouch", "comagic": "CoMagic", "roistat": "Roistat", "mango": "Mango Office",
                   "callibri": "Callibri"}

_METRIKA_ID = re.compile(r"(?:ym\(\s*|mc\.yandex\.ru/watch/|metrika/tag\.js\?id=)(\d{5,10})")
_GA_ID = re.compile(r"\b(G-[A-Z0-9]{6,12}|UA-\d{4,10}-\d{1,3})\b")
_COPY_YEAR = re.compile(r"(?:©|&copy;|\(c\)|copyright)[^0-9]{0,40}((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?",
                        re.I)
_YEAR = re.compile(r"\b(20\d{2})\b")
_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_ANCHOR = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)


def _found(hay: str, markers: dict[str, tuple]) -> list[str]:
    return sorted(name for name, marks in markers.items() if any(m in hay for m in marks))


def footer_year(html: str, now_year: int) -> int | None:
    """Год из «© 2019–2024» (берём последний), иначе самый поздний год в последних 15% текста страницы."""
    text = unescape(_TAGS.sub(" ", html))
    years = []
    for m in _COPY_YEAR.finditer(text):
        years.append(int(m.group(2) or m.group(1)))
    if not years:
        tail = text[int(len(text) * 0.85):]
        years = [int(y) for y in _YEAR.findall(tail)]
    years = [y for y in years if 1995 <= y <= now_year + 1]
    return max(years) if years else None


def has_privacy_policy(html: str) -> bool:
    for inner in _ANCHOR.findall(html):
        text = re.sub(r"<[^>]+>", " ", unescape(inner)).lower()
        if "политик" in text and ("конфиденц" in text or "персональн" in text or "обработк" in text):
            return True
        if "персональных данных" in text or "privacy" in text:
            return True
    return False


def profile(html: str, now_year: int) -> dict:
    hay = (html or "").lower()
    raw = html or ""
    metrika_ids = sorted(set(_METRIKA_ID.findall(raw)))
    has_metrika = bool(metrika_ids) or any(m in hay for m in METRIKA_MARKERS)
    ga_ids = sorted(set(_GA_ID.findall(raw)))
    has_ga = bool(ga_ids) or any(m in hay for m in GA_MARKERS)
    platform = next((name for name, marks in PLATFORM_MARKERS if any(m in hay for m in marks)), None)
    year = footer_year(raw, now_year)
    return {
        "has_metrika": has_metrika,
        "metrika_ids": metrika_ids[:5],
        "has_ga": has_ga,
        "ga_ids": ga_ids[:5],
        "has_gtm": any(m in hay for m in GTM_MARKERS),
        "calltracking": _found(hay, CALLTRACKING_MARKERS),
        "chats": _found(hay, {**CHAT_MARKERS, **EXTRA_CHATS}),
        "crm_forms": _found(hay, CRM_MARKERS),
        "platform": platform,
        "platform_title": PLATFORM_TITLES.get(platform) if platform else None,
        "footer_year": year,
        "has_quiz": any(m in hay for m in QUIZ_MARKERS),
        "has_privacy_policy": has_privacy_policy(raw),
    }
