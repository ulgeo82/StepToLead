"""Что видно на сайте компании: контакты, ИНН, квиз, CRM, Метрика. Чистая логика: html -> факты, без сети."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from .normalize import normalize_domain, normalize_phone, validate_inn

# Сигнатуры подключаемых сервисов (ищем в html и адресах скриптов).
QUIZ_MARKERS = ("marquiz", "quizgo", "enquiz", "quizbot", "flexbe-quiz", "leadsquiz", "quiz.mk", "kviz", "quiz-calc")
CALC_MARKERS = ("калькулятор", "рассчитать стоимость", "расчёт стоимости", "расчет стоимости")
CRM_MARKERS = {
    "amocrm": ("amocrm.ru", "amo_forms", "gso.amocrm", "amocrm.com", "kommo.com"),
    "bitrix24": ("bitrix24.ru/b", "bitrix24.ru/crm", "b24-", "cdn-ru.bitrix24", "bitrix24.com"),
}
CHAT_MARKERS = {
    "jivo": ("jivosite", "jivo.ru", "code.jivo"),
    "envybox": ("envybox",),
    "callbackhunter": ("callbackhunter",),
    "talk-me": ("talk-me.ru",),
    "bitrix24": ("b24-widget", "crm/site_button"),
}
METRIKA_MARKERS = ("mc.yandex.ru/metrika", "mc.yandex.ru/watch", "ym(")

# Страницы, где обычно лежат реквизиты и контакты.
PAGE_HINTS = ("contact", "kontakt", "контакт", "about", "o-nas", "o-kompanii", "о-компании", "о нас", "о компании",
              "rekvizit", "реквизит", "privacy", "politik", "policy", "политик", "oferta", "оферт", "requisites")

_EMAIL = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-zа-я]{2,12})(?![\w-])", re.I)
_PHONE = re.compile(r"(?<![\d+])(?:\+7|8)[\s\-‐–(]*\d{3}[\s\-‐–)]*\d{3}[\s\-‐–]*\d{2}[\s\-‐–]*\d{2}(?!\d)")
_INN = re.compile(r"ИНН[\s:№/]*(?:КПП[\s:/]*)?(\d{10}|\d{12})(?!\d)", re.I)
_INN_KPP = re.compile(r"ИНН\s*/\s*КПП[\s:]*(\d{10})\s*/\s*\d{9}", re.I)
_OGRN = re.compile(r"ОГРН(?:ИП)?[\s:№]*(\d{13}|\d{15})(?!\d)", re.I)
_HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#][^"']*)["'][^>]*>(.*?)</a>""", re.I | re.S)
_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>|<[^>]+>", re.I | re.S)
_SRC = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
_BAD_EMAIL_TAIL = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
_TG_SKIP = {"share", "joinchat", "addstickers", "iv", "s", "proxy", "socks", "c"}


@dataclass
class SiteFacts:
    phones: set[str] = field(default_factory=set)
    whatsapp: set[str] = field(default_factory=set)      # нормализованные телефоны
    telegram: set[str] = field(default_factory=set)      # логины без @
    emails: set[str] = field(default_factory=set)
    inns: set[str] = field(default_factory=set)
    ogrns: set[str] = field(default_factory=set)
    has_quiz: bool = False
    crm: set[str] = field(default_factory=set)
    chats: set[str] = field(default_factory=set)
    has_metrika: bool = False
    pages: list[str] = field(default_factory=list)       # кандидаты на обход (абсолютные url того же домена)

    def merge(self, other: "SiteFacts") -> None:
        for name in ("phones", "whatsapp", "telegram", "emails", "inns", "ogrns", "crm", "chats"):
            getattr(self, name).update(getattr(other, name))
        self.has_quiz = self.has_quiz or other.has_quiz
        self.has_metrika = self.has_metrika or other.has_metrika


def html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAGS.sub(" ", html)))


def _wa_phone(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("wa.me", "www.wa.me"):
        return normalize_phone(parts.path.strip("/").split("/")[0])
    if host.endswith("whatsapp.com"):
        q = parse_qs(parts.query)
        return normalize_phone((q.get("phone") or [""])[0])
    if url.lower().startswith("whatsapp://"):
        return normalize_phone((parse_qs(parts.query).get("phone") or [""])[0])
    return None


def _tg_login(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("t.me", "www.t.me", "telegram.me", "www.telegram.me"):
        return None
    first = parts.path.strip("/").split("/")[0]
    if not first or first.lower() in _TG_SKIP or first.startswith("+"):
        return None
    login = first.lstrip("@").lower()
    return login if re.fullmatch(r"[a-z0-9_]{5,32}", login) else None


def extract(html: str, base_url: str) -> SiteFacts:
    facts = SiteFacts()
    low = html.lower()
    text = html_to_text(html)
    base_domain = normalize_domain(base_url)

    for raw_href, inner in _HREF.findall(html):
        href = unescape(raw_href.strip())
        hl = href.lower()
        if hl.startswith("tel:"):
            p = normalize_phone(unquote(href[4:]))
            if p:
                facts.phones.add(p)
        elif hl.startswith("mailto:"):
            email = unquote(href[7:]).split("?")[0].strip().lower()
            if _EMAIL.fullmatch(email):
                facts.emails.add(email)
        else:
            absolute = urljoin(base_url, href)
            wa = _wa_phone(absolute)
            if wa:
                facts.whatsapp.add(wa)
                continue
            tg = _tg_login(absolute)
            if tg:
                facts.telegram.add(tg)
                continue
            if normalize_domain(absolute) == base_domain and absolute.startswith(("http://", "https://")):
                label = f"{hl} {html_to_text(inner).lower()}"
                if any(h in label for h in PAGE_HINTS):
                    clean = absolute.split("#")[0]
                    if clean not in facts.pages and clean.rstrip("/") != base_url.rstrip("/"):
                        facts.pages.append(clean)

    for m in _PHONE.finditer(text):
        p = normalize_phone(m.group(0))
        if p:
            facts.phones.add(p)
    for m in _EMAIL.finditer(text):
        email = m.group(1).lower()
        if not email.endswith(_BAD_EMAIL_TAIL):
            facts.emails.add(email)
    for rx in (_INN_KPP, _INN):
        for m in rx.finditer(text):
            inn = validate_inn(m.group(1))
            if inn:
                facts.inns.add(inn)
    for m in _OGRN.finditer(text):
        facts.ogrns.add(m.group(1))

    sources = " ".join(_SRC.findall(html)).lower() + " " + low
    facts.has_quiz = any(m in sources for m in QUIZ_MARKERS) or any(m in text.lower() for m in CALC_MARKERS)
    for name, markers in CRM_MARKERS.items():
        if any(m in sources for m in markers):
            facts.crm.add(name)
    for name, markers in CHAT_MARKERS.items():
        if any(m in sources for m in markers):
            facts.chats.add(name)
    facts.has_metrika = any(m in sources for m in METRIKA_MARKERS)
    return facts
