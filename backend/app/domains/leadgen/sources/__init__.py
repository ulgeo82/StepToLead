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

ORDER = ("site_tech", "serp", "hiring", "find_site", "site_contacts", "email_check", "name_clean", "dadata",
         "fns_revenue", "fit_ai", "personalize")


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


def variable_specs() -> dict[str, tuple[str, str]]:
    """{{переменная}} -> (код источника, поле в lg_enrichments.result) — по `variables` источников."""
    out: dict[str, tuple[str, str]] = {}
    for code, adapter in REGISTRY.items():
        for spec in adapter.variables:
            out.setdefault(spec["key"], (code, spec.get("field") or spec["key"]))
    return out


async def variable_values(db, company_id: int) -> dict:
    """Значения переменных источников для письма. Пусто — переменной нет (сработает значение по умолчанию)."""
    from sqlalchemy import select
    from app.domains.leadgen.models import LgEnrichment
    specs = variable_specs()
    if not specs:
        return {}
    rows = dict((await db.execute(select(LgEnrichment.step, LgEnrichment.result).where(
        LgEnrichment.company_id == company_id, LgEnrichment.status == "done",
        LgEnrichment.step.in_({c for c, _ in specs.values()})))).all())
    out = {}
    for key, (code, field) in specs.items():
        value = (rows.get(code) or {}).get(field)
        if value not in (None, "", []):
            out[key] = value if isinstance(value, str) else str(value)
    return out


def _register_variables() -> None:
    from app.domains.leadgen.core import sequence as seq_core
    for key in variable_specs():
        if key not in seq_core.VARIABLES and key not in seq_core.EXTRA_VARIABLES:
            seq_core.EXTRA_VARIABLES.append(key)


_register_variables()
