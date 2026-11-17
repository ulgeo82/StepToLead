"""Client-facing, read-only outreach. The session is the only workspace selector."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_portal_user
from app.db import get_db
from app.models.marketing import ClientWorkspace, PortalUser
from app.services import outreach_inbox as inbox, portal_outreach as read

router = APIRouter(prefix="/portal/outreach", tags=["portal-outreach"])
Channel = Literal["email", "telegram"]


async def workspace(user: PortalUser = Depends(require_portal_user), db: AsyncSession = Depends(get_db)) -> int:
    row = await db.get(ClientWorkspace, user.workspace_id)
    if not row or row.status != "active" or not row.show_outreach:
        raise HTTPException(403, "Раздел «Аутрич» не подключён")
    return user.workspace_id


@router.get("/summary")
async def summary(ws: int = Depends(workspace), db: AsyncSession = Depends(get_db)):
    return await read.summary(db, ws)


@router.get("/contacted")
async def contacted(channel: Channel | None = None, q: str | None = Query(None, max_length=200),
                    page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                    ws: int = Depends(workspace), db: AsyncSession = Depends(get_db)):
    return await read.contacted(db, ws, channel=channel, q=q, page=page, per_page=per_page)


@router.get("/inbox")
async def listing(channel: Channel | None = None, q: str | None = Query(None, max_length=200),
                  page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                  ws: int = Depends(workspace), db: AsyncSession = Depends(get_db)):
    data = await inbox.listing(db, [ws], None, status="all", channel=channel, q=q, page=page,
                               per_page=per_page, client_view=True)
    return {"total": data["total"], "items": await read.visible_threads(db, ws, data["items"])}


@router.get("/inbox/{key}")
async def detail(key: str, ws: int = Depends(workspace), db: AsyncSession = Depends(get_db)):
    conv = await inbox.find(db, key, [ws])
    data = await inbox.detail(db, conv, client_view=True)
    return (await read.visible_threads(db, ws, [data]))[0]
