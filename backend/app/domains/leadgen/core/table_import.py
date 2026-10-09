"""Разбор списка компаний из файла или вставленного текста: CSV (`,` `;` таб), Excel, просто домены по строке.

Колонки узнаём по названию (как их называют выгрузки 2ГИС, Polza, Excel-таблицы вручную):
сайт / название / город / ниша / телефон / email / ИНН / Telegram / WhatsApp.
Без заголовка каждая строка — домен или ссылка (лишние ячейки игнорируем).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from app.domains.leadgen.core.normalize import normalize_domain, normalize_phone, validate_inn

MAX_ROWS = 2000
COLUMNS = {
    "domain": ("сайт", "site", "website", "web", "url", "домен", "domain", "адрес сайта", "ссылка"),
    "name": ("название", "наименование", "компания", "организация", "name", "company", "бренд"),
    "legal_name": ("юрлицо", "юр. лицо", "юридическое", "legal"),
    "city": ("город", "city", "населенный пункт", "населённый пункт"),
    "niche": ("ниша", "рубрика", "категория", "сфера", "niche", "category", "вид деятельности"),
    "phone": ("телефон", "phone", "тел", "мобильный", "phones"),
    "email": ("email", "e-mail", "почта", "mail", "эл. почта", "электронная почта"),
    "inn": ("инн", "inn"),
    "telegram": ("telegram", "телеграм", "tg"),
    "whatsapp": ("whatsapp", "ватсап", "вотсап", "wa"),
}
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SPLIT = re.compile(r"[,;\n|]+|\s{2,}")


@dataclass
class Row:
    line: int
    domain: str | None = None
    name: str | None = None
    legal_name: str | None = None
    city: str | None = None
    niche: str | None = None
    inn: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    telegram: list[str] = field(default_factory=list)
    whatsapp: list[str] = field(default_factory=list)


@dataclass
class Parsed:
    rows: list[Row]
    invalid: list[dict]          # {"line", "reason", "value"}
    columns: dict[str, int]      # какие колонки узнали (для подсказки на экране)
    truncated: bool = False


def _column_for(header: str) -> str | None:
    h = re.sub(r"\s+", " ", (header or "").strip().lower().replace("ё", "е")).strip(" :*")
    if not h or (" " not in h and "." in h and normalize_domain(h)):
        return None  # пустая ячейка или сам домен — это не заголовок
    for key, names in COLUMNS.items():
        if any(h == n or h.startswith(n + " ") or h.startswith(n + ",") for n in names):
            return key
    for key, names in COLUMNS.items():  # «Сайт компании», «Телефон 1» и т. п.
        if any(n in h for n in names if len(n) > 3):
            return key
    return None


def _cells_from_text(text: str) -> list[list[str]]:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if "\t" in sample else ";" if sample.count(";") > sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def _cells_from_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append(["" if v is None else str(v).strip() for v in row])
        if len(out) > MAX_ROWS + 1:
            break
    wb.close()
    return out


def _split(value: str) -> list[str]:
    return [p.strip() for p in _SPLIT.split(value or "") if p.strip()]


def _first_domain(value: str) -> str | None:
    for part in _split(value) or [value]:
        part = part.strip()
        if "@" in part and "/" not in part:
            continue  # это почта, а не сайт
        d = normalize_domain(part)
        if d and "." in d:
            return d
    return None


def _fill(row: Row, key: str, value: str) -> None:
    value = (value or "").strip()
    if not value:
        return
    if key == "domain":
        row.domain = row.domain or _first_domain(value)
    elif key == "inn":
        row.inn = row.inn or validate_inn(re.sub(r"\D", "", value)[:12])
    elif key == "phone":
        row.phones += [p for p in (normalize_phone(x) for x in _split(value)) if p and p not in row.phones]
    elif key == "email":
        row.emails += [e.lower() for e in _EMAIL.findall(value) if e.lower() not in row.emails]
    elif key == "telegram":
        row.telegram += [t for t in _split(value) if t not in row.telegram]
    elif key == "whatsapp":
        row.whatsapp += [p for p in (normalize_phone(x) for x in _split(value)) if p and p not in row.whatsapp]
    elif getattr(row, key) in (None, ""):
        setattr(row, key, value[:200])


def parse(*, text: str | None = None, xlsx: bytes | None = None) -> Parsed:
    cells = _cells_from_xlsx(xlsx) if xlsx is not None else _cells_from_text(text or "")
    cells = [[c.strip() for c in r] for r in cells if any(c.strip() for c in r)]
    if not cells:
        return Parsed([], [], {})
    mapping = {i: _column_for(h) for i, h in enumerate(cells[0])}
    mapping = {i: k for i, k in mapping.items() if k}
    has_header = len(mapping) >= 1 and any(k in ("domain", "name", "inn", "phone", "email") for k in mapping.values())
    body = cells[1:] if has_header else cells
    start = 2 if has_header else 1
    truncated = len(body) > MAX_ROWS
    rows, invalid = [], []
    for n, cells_row in enumerate(body[:MAX_ROWS], start=start):
        row = Row(line=n)
        if has_header:
            for i, key in mapping.items():
                if i < len(cells_row):
                    _fill(row, key, cells_row[i])
        else:
            # Без заголовка: ищем сайт в любой ячейке, почты и телефоны — тоже.
            for value in cells_row:
                if not row.domain:
                    row.domain = _first_domain(value)
                row.emails += [e.lower() for e in _EMAIL.findall(value) if e.lower() not in row.emails]
        if not row.domain and row.emails:
            host = row.emails[0].split("@")[-1]
            if host not in FREE_MAIL:
                row.domain = normalize_domain(host)
        if not (row.domain or row.inn or row.phones):
            invalid.append({"line": n, "reason": "нет сайта, ИНН или телефона", "value": " | ".join(c for c in cells_row if c)[:120]})
            continue
        rows.append(row)
    columns = {}
    for k in mapping.values():
        columns[k] = columns.get(k, 0) + 1
    return Parsed(rows, invalid, columns if has_header else {}, truncated)


FREE_MAIL = {"gmail.com", "mail.ru", "yandex.ru", "ya.ru", "bk.ru", "list.ru", "inbox.ru", "rambler.ru",
             "outlook.com", "hotmail.com", "icloud.com", "yahoo.com", "internet.ru", "yandex.com"}
