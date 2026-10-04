"""Deleting a client company from the agency admin.

A company is removed from every list and switched off completely: logins of its staff and their sessions,
ad cabinets, chats, telephony, webhooks, site widgets and background work (projects archived). Its CRM history
is kept, so a company deleted by mistake comes back as it was with «Восстановить»: what was on before deletion
is switched on again (snapshot in app_settings).
"""
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import AdConnection, ClientWorkspace, LeadInboundSource, PortalSession, PortalUser, Project
from app.models.messaging import MessagingChannel
from app.models.system import AppSetting
from app.models.telephony import TelephonyConnection
from app.models.website import WebsiteSite

SUFFIX = " · удалена #"


def key(workspace_id: int) -> str:
    return f"ws_deleted:{workspace_id}"


def original_name(workspace: ClientWorkspace) -> str:
    return workspace.name.split(SUFFIX)[0]


async def delete_workspace(db: AsyncSession, workspace: ClientWorkspace) -> dict:
    """Does not commit."""
    wid = workspace.id
    users = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == wid))).all()
    ads = (await db.scalars(select(AdConnection).where(AdConnection.workspace_id == wid))).all()
    channels = (await db.scalars(select(MessagingChannel).where(MessagingChannel.workspace_id == wid))).all()
    phones = (await db.scalars(select(TelephonyConnection).where(TelephonyConnection.workspace_id == wid))).all()
    hooks = (await db.scalars(select(LeadInboundSource).where(LeadInboundSource.workspace_id == wid))).all()
    sites = (await db.scalars(select(WebsiteSite).where(WebsiteSite.workspace_id == wid))).all()
    projects = (await db.scalars(select(Project).where(Project.workspace_id == wid))).all()
    snapshot = {"name": workspace.name, "status": workspace.status, "deleted_at": datetime.now(timezone.utc).isoformat(),
                "users": [u.id for u in users if u.active], "ads": {str(a.id): a.status for a in ads if a.status != "disconnected"},
                "channels": [c.id for c in channels if c.active], "telephony": [t.id for t in phones if t.active],
                "hooks": [h.id for h in hooks if h.active], "sites": [s.id for s in sites if s.active],
                "projects": {str(p.id): p.status for p in projects}}
    for u in users:
        u.active = False
    if users:
        await db.execute(delete(PortalSession).where(PortalSession.user_id.in_([u.id for u in users])))
    for a in ads:
        a.status = "disconnected"
    for row in [*channels, *phones, *hooks, *sites]:
        row.active = False
    for p in projects:
        p.status = "archived"
    workspace.status = "deleted"
    workspace.name = f"{original_name(workspace)}{SUFFIX}{wid}"[:180]  # frees the name for a new client
    existing = await db.get(AppSetting, key(wid))
    if existing:
        existing.value = snapshot
    else:
        db.add(AppSetting(key=key(wid), value=snapshot))
    return {"users": len(snapshot["users"]), "integrations": len(snapshot["ads"]) + len(snapshot["channels"]) + len(snapshot["telephony"])}


async def restore_workspace(db: AsyncSession, workspace: ClientWorkspace) -> None:
    """Does not commit. Raises ValueError when the original name is taken by another client."""
    snap = (await db.get(AppSetting, key(workspace.id)))
    data = snap.value if snap else {}
    name = original_name(workspace)
    clash = await db.scalar(select(ClientWorkspace.id).where(ClientWorkspace.name == name, ClientWorkspace.id != workspace.id))
    if clash:
        raise ValueError(f"Название «{name}» уже занято другим клиентом — переименуйте его и повторите")
    workspace.name, workspace.status = name, data.get("status") or "active"
    if workspace.status == "deleted":
        workspace.status = "active"
    ids = lambda field: set(data.get(field) or [])
    for u in (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == workspace.id))).all():
        if u.id in ids("users"):
            u.active = True
    ads = data.get("ads") or {}
    for a in (await db.scalars(select(AdConnection).where(AdConnection.workspace_id == workspace.id))).all():
        if str(a.id) in ads:
            a.status = ads[str(a.id)]
    for model, field in ((MessagingChannel, "channels"), (TelephonyConnection, "telephony"), (LeadInboundSource, "hooks"), (WebsiteSite, "sites")):
        for row in (await db.scalars(select(model).where(model.workspace_id == workspace.id))).all():
            if row.id in ids(field):
                row.active = True
    statuses = data.get("projects") or {}
    for p in (await db.scalars(select(Project).where(Project.workspace_id == workspace.id))).all():
        p.status = statuses.get(str(p.id)) or "active"
    if snap:
        await db.delete(snap)
