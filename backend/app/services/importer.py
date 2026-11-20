import csv
import io
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook


@dataclass
class ImportedLead:
    username: str
    first_message: str


ALIASES = {
    "username": ("username", "user", "юзернейм", "ник", "логин", "telegram", "телеграм", "tg", "контакт"),
    "first_message": ("first_message", "first message", "message", "text", "сообщение", "первое сообщение", "текст"),
}


def _normalize_headers(row: list[object]) -> list[str]:
    """Заголовки «как люди пишут»: регистр, пробелы, неразрывные пробелы, BOM, @ — не важны; синонимы — к своим."""
    out = []
    for value in row:
        name = str(value or "").replace("\ufeff", "").replace("\xa0", " ").strip().lower().lstrip("@")
        name = " ".join(name.split())
        for canon, names in ALIASES.items():
            if name in names or name.replace(" ", "_") == canon:
                name = canon
                break
        out.append(name)
    return out


def _to_leads(rows: list[list[object]]) -> tuple[list[ImportedLead], list[str]]:
    rows = [r for r in rows if any(str(v or "").strip() for v in r)]   # пустые строки (в т.ч. над шапкой) — мимо
    if not rows:
        raise ValueError("Файл пуст")
    headers = _normalize_headers(rows[0])
    required = {"username", "first_message"}
    if not required.issubset(headers):
        found = ", ".join(h for h in headers if h) or "пусто"
        raise ValueError(f"Нужны колонки username и first_message. В первой строке файла: {found}")

    username_index = headers.index("username")
    message_index = headers.index("first_message")
    leads: list[ImportedLead] = []
    errors: list[str] = []
    seen: set[str] = set()

    for line_number, row in enumerate(rows[1:], start=2):
        values = list(row) + [""] * (len(headers) - len(row))
        username = str(values[username_index] or "").strip().lstrip("@").lower()
        message = str(values[message_index] or "").strip()
        if not username or not message:
            errors.append(f"Строка {line_number}: пустой username или first_message")
            continue
        if username in seen:
            errors.append(f"Строка {line_number}: дубликат @{username}")
            continue
        seen.add(username)
        leads.append(ImportedLead(username=username, first_message=message))
    return leads, errors


def parse_leads_file(filename: str, content: bytes) -> tuple[list[ImportedLead], list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = content.decode("utf-8-sig")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        rows = [list(row) for row in csv.reader(io.StringIO(text), dialect)]
        return _to_leads(rows)
    if suffix == ".xlsx":
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        # Многие программы пишут в файл неверный размер листа (например, A1:A1) — тогда openpyxl в режиме
        # read_only видит только первую колонку. Сбрасываем и читаем лист целиком.
        sheet.reset_dimensions()
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        return _to_leads(rows)
    raise ValueError("Поддерживаются только CSV и XLSX")

