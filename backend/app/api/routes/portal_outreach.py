"""Client-facing, read-only outreach. The session is the only workspace selector."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
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
async def detail(key: str, ws: int = Depends(workspace), db: AsyncSession = Depends(get_db),
                 user: PortalUser = Depends(require_portal_user)):
    conv = await inbox.find(db, key, [ws])
    data = await inbox.detail(db, conv, client_view=True)
    out = (await read.visible_threads(db, ws, [data]))[0]
    allowed = bool(user.can_reply_outreach)
    out["can_reply"] = allowed and data["can_reply"]
    out["reply_blocker"] = None if not allowed else data["reply_blocker"]
    lock = data.get("locked_by")
    out["locked_by"] = {"name": lock["name"], "until": lock["until"], "me": lock["id"] == -user.id} if lock else None
    return out


def replier(user: PortalUser = Depends(require_portal_user)) -> PortalUser:
    if not user.can_reply_outreach:
        raise HTTPException(403, "Отвечать лидам вам не разрешено — попросите агентство")
    return user


@router.post("/inbox/{key}/lock")
async def lock(key: str, ws: int = Depends(workspace), user: PortalUser = Depends(replier),
               db: AsyncSession = Depends(get_db)):
    conv = await inbox.find(db, key, [ws])
    until = await inbox.lock(db, await inbox.state(db, conv.thread, conv.ws), inbox.portal_actor(user))
    await db.commit()
    return {"until": inbox._iso(until)}


@router.delete("/inbox/{key}/lock", status_code=204)
async def unlock(key: str, ws: int = Depends(workspace), user: PortalUser = Depends(replier),
                 db: AsyncSession = Depends(get_db)):
    from fastapi import Response
    conv = await inbox.find(db, key, [ws])
    st = await inbox.state(db, conv.thread, conv.ws, create=False)
    if st is not None:
        inbox.unlock(st, -user.id)
        await db.commit()
    return Response(status_code=204)


class ReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4096)


@router.post("/inbox/{key}/reply")
async def reply(key: str, payload: ReplyIn, ws: int = Depends(workspace), user: PortalUser = Depends(replier),
                db: AsyncSession = Depends(get_db)):
    conv = await inbox.find(db, key, [ws])
    transport = None
    if conv.channel == "email":
        from app.domains.leadgen import routes as leadgen_routes
        transport = leadgen_routes.mail_transport_factory()
    try:
        message = await inbox.reply(db, conv, inbox.portal_actor(user), payload.text.strip(), transport=transport)
    except inbox.ReplyError as exc:
        await db.rollback()
        raise HTTPException(exc.status, str(exc)) from exc
    return {"id": message["id"], "text": message["text"], "at": message["at"], "direction": "out",
            "author": user.display_name or user.username}
