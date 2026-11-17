"""Пространство клиента для Telegram-аутрича (этап B, docs/OUTREACH_INBOX.md).

Тот же механизм, что в лидогене (app/domains/leadgen/space.py): клиент приходит заголовком X-Workspace-Id или
?ws=, зависимость space.bind кладёт его в ContextVar. Здесь — только чтение и фильтры для Telegram-таблиц
(telegram_accounts, proxies пула «Аутрич», campaigns; диалоги, сообщения, очередь и парсер — через кампанию или
аккаунт). Записи с workspace_id = NULL — старые, до переноса: считаются агентскими.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_

from app.core.config import settings as app_settings


def home() -> int | None:
    return app_settings.agency_workspace_id or None


def current() -> int | None:
    """Выбранный клиент, иначе агентство. None — портал без AGENCY_WORKSPACE_ID (разработка): без разделения."""
    from app.domains.leadgen import space
    return space._current.get() or home()


def scope(query, column):
    ws = current()
    if ws is None:
        return query
    if ws == home():
        return query.where(or_(column == ws, column.is_(None)))
    return query.where(column == ws)


def belongs(column, workspace_id: int | None):
    """SQL-условие «запись пространства workspace_id» — для фоновых задач, где нет контекста запроса."""
    if workspace_id is None:
        return column.is_(None) | column.is_not(None)
    if workspace_id == home():
        return or_(column == workspace_id, column.is_(None))
    return column == workspace_id


def owns(row_workspace_id: int | None) -> bool:
    ws = current()
    if ws is None:
        return True
    return row_workspace_id == ws or (row_workspace_id is None and ws == home())


def require(row, message: str = "Не найдено"):
    """404, если записи нет или она другого клиента (не раскрываем, что такая есть)."""
    if row is None or not owns(getattr(row, "workspace_id", None)):
        raise HTTPException(404, message)
    return row


def same_space(a: int | None, b: int | None) -> bool:
    h = home()
    return (a or h) == (b or h)
