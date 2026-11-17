"""Пространство клиента для лидогенерации и аутрича (docs/MODEL.md, docs/PLAN.md — этап 1).

Раздел работает «внутри клиента»: клиент приходит заголовком X-Workspace-Id или параметром ?ws= (для ссылок и
скачивания файлов). Без них — пространство агентства (settings.agency_workspace_id), как было до этапа 1.
Пространство запроса лежит в ContextVar: зависимость bind() ставит его до обработчика, routes.workspace_id()
читает. Фоновые задачи пространство берут из своих строк (run.workspace_id), а не отсюда.
"""
from __future__ import annotations

from contextvars import ContextVar

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.core.config import settings as app_settings
from app.db import get_db
from app.models.access import AdminUser
from app.models.marketing import ClientWorkspace

HEADER = "x-workspace-id"
PARAM = "ws"
_current: ContextVar[int | None] = ContextVar("leadgen_space", default=None)


def home() -> int:
    """Пространство по умолчанию — клиент №0 (агентство)."""
    if not app_settings.agency_workspace_id:
        raise HTTPException(503, "Не задан AGENCY_WORKSPACE_ID — лидогенерация работает в аккаунте агентства")
    return app_settings.agency_workspace_id


def current() -> int:
    return _current.get() or home()


async def active_workspace(db: AsyncSession, workspace_id: int) -> ClientWorkspace | None:
    row = await db.get(ClientWorkspace, workspace_id)
    return row if row is not None and row.status == "active" else None


async def bind(request: Request, db: AsyncSession = Depends(get_db),
               admin: AdminUser = Depends(require_admin)) -> None:
    """Ставит пространство запроса. Сначала вход администратора: посторонний не узнаёт, какие клиенты есть."""
    raw = request.headers.get(HEADER) or request.query_params.get(PARAM)
    if raw in (None, ""):
        _current.set(None)
        return
    if not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(422, "Клиент указан неверно")
    if await active_workspace(db, int(raw)) is None:
        raise HTTPException(404, "Клиент не найден или отключён")
    _current.set(int(raw))


async def listing(db: AsyncSession) -> list[dict]:
    """Клиенты для переключателя: активные, агентство первым. Счётчики — чтобы видеть, где уже идёт работа."""
    from app.domains.leadgen.models import LgCampaign, LgCompany, LgMailbox
    rows = (await db.scalars(select(ClientWorkspace).where(ClientWorkspace.status == "active")
                             .order_by(ClientWorkspace.name))).all()

    async def counts(model, *where) -> dict[int, int]:
        return dict((await db.execute(select(model.workspace_id, func.count(model.id)).where(*where)
                                      .group_by(model.workspace_id))).all())

    companies = await counts(LgCompany)
    mailboxes = await counts(LgMailbox, LgMailbox.is_active.is_(True))
    campaigns = await counts(LgCampaign, LgCampaign.status == "active")
    home_id = app_settings.agency_workspace_id
    items = [{"id": w.id, "name": w.name, "is_home": w.id == home_id, "companies": int(companies.get(w.id, 0)),
              "mailboxes": int(mailboxes.get(w.id, 0)), "active_campaigns": int(campaigns.get(w.id, 0))} for w in rows]
    return sorted(items, key=lambda i: (not i["is_home"], i["name"].lower()))
