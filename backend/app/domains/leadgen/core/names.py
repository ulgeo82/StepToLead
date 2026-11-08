"""Presentation-only company names. This does not change deduplication/name_key rules."""
import re

from .normalize import _LEGAL_FORMS, clean_company_name as strip_legal


def clean_company_name(raw: str) -> tuple[str, str | None]:
    original = str(raw or "")
    # A sole proprietor's full name is not a brand. A verified site brand is handled by the enricher.
    if re.search(r"^\s*(?:ИП\b|индивидуальный\s+предприниматель\b)|\bИП\s*$", original, re.I):
        return original, "name_clean:ip"
    name = strip_legal(original)
    if not name or name.casefold() in _LEGAL_FORMS:
        return original, "name_clean:empty"

    def word(match):
        value = match[0]
        return value.capitalize() if value.isupper() and len(value) > 4 else value

    name = re.sub(r"[A-Za-zА-Яа-яЁё]+", word, name)
    return name, None
