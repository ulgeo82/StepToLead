"""Источники компаний для лидогенерации. Реестр собирается из модулей этой папки: у каждого — объект ADAPTER.

Новый источник = файл sources/<code>.py с ADAPTER = MySource() и имя модуля в ORDER. Раннер, настройки и API
подхватят его сами. Модуль, которого ещё нет, пропускается.
"""
from __future__ import annotations

import importlib

from app.domains.leadgen.sources.base import (  # noqa: F401 — общий импорт
    Candidate, CompanySource, CompanyView, Context, Enrichment, SignalIn, SourceFormatError, SourceSkipped, limited,
    short_error,
)

ORDER = ("site_tech", "serp", "hiring", "find_site", "site_contacts", "email_check", "name_clean")


def _load() -> dict[str, CompanySource]:
    found: dict[str, CompanySource] = {}
    for name in ORDER:
        try:
            module = importlib.import_module(f"app.domains.leadgen.sources.{name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(f".{name}"):
                continue
            raise
        adapter = getattr(module, "ADAPTER", None)
        if isinstance(adapter, CompanySource) and adapter.code:
            found[adapter.code] = adapter
    return found


REGISTRY: dict[str, CompanySource] = _load()


def get(code: str) -> CompanySource | None:
    return REGISTRY.get(code)
