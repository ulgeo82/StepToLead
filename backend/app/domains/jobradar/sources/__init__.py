"""Площадки радара вакансий. Реестр собирается из модулей этой папки: у каждого — объект ADAPTER.

Добавить площадку: файл sources/<code>.py с ADAPTER = MyAdapter() и имя модуля в ORDER. Модуль, которого
ещё нет, просто пропускается — площадки можно дописывать параллельно.
"""
from __future__ import annotations

import importlib
import logging

HH_TOKEN_HINT = "нужен токен приложения (dev.hh.ru)"

from app.domains.jobradar.sources.base import (  # noqa: F401 — общий импорт для остального модуля
    UA, Adapter, Context, SourceFormatError, SourceSkipped, Vacancy, fingerprint, has_word, looks_remote, norm,
    parse_dt, parse_rfc822, short_error, strip_tags, to_int,
)

logger = logging.getLogger("uvicorn.error.jobradar")

ORDER = ("trudvsem", "hh", "habr", "getmatch", "superjob", "flru", "telegram_channels")


def _load() -> dict[str, Adapter]:
    found: dict[str, Adapter] = {}
    for name in ORDER:
        try:
            module = importlib.import_module(f"app.domains.jobradar.sources.{name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(f".{name}"):
                continue   # площадку ещё не написали
            raise
        adapter = getattr(module, "ADAPTER", None)
        if isinstance(adapter, Adapter) and adapter.code:
            found[adapter.code] = adapter
    return found


REGISTRY: dict[str, Adapter] = _load()


def get(code: str) -> Adapter | None:
    return REGISTRY.get(code)


def names() -> dict[str, str]:
    return {code: a.title for code, a in REGISTRY.items()}


ALL = tuple(REGISTRY)
