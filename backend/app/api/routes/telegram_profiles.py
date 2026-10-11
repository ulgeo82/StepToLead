"""Customer-profile login; no agency TelegramAccount rows are created."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.routes.crm import project_for
from app.api.routes.messaging import can_manage_channels, channel_json
from app.core.access import check_origin, require_portal_user
from app.db import get_db
from app.models.messaging import MessagingChannel
from app.services import telegram_profiles
from app.services.messaging import ChannelError

router = APIRouter(prefix="/crm", dependencies=[Depends(require_portal_user)])


class ProfileIn(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    phone: str = Field(pattern=r"^\+[0-9]{8,15}$")
    mode: str = Field(default="corporate", pattern=r"^(corporate|personal)$")


class ConfirmIn(BaseModel):
    code: str | None = Field(default=None, max_length=20)
    password: str | None = Field(default=None, max_length=300)


async def managed(db, user, channel_id):
    channel = await db.get(MessagingChannel, channel_id)
    if not channel or channel.workspace_id != user.workspace_id or channel.kind != "telegram_user":
        raise HTTPException(404, "Telegram-профиль не найден")
    await project_for(db, user, channel.project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настраивать каналы может руководитель или владелец")
    return channel


@router.post("/projects/{project_id}/telegram-profile", status_code=201)
async def create(project_id: int, payload: ProfileIn, request: Request,
                 db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настраивать каналы может руководитель или владелец")
    from app.services import plans
    await plans.require(db, project.workspace_id, "all_chats")
    rows = (await db.scalars(select(MessagingChannel).where(
        MessagingChannel.workspace_id == user.workspace_id, MessagingChannel.kind == "telegram_user"))).all()
    channel = next((row for row in rows if (row.config or {}).get("phone") == payload.phone), None)
    if channel and channel.project_id != project_id:
        raise HTTPException(409, "Профиль уже подключён к другому проекту этой компании")
    if channel and channel.active and channel.status == "connected":
        raise HTTPException(409, "Профиль уже подключён. Сначала выключите канал.")
    if channel is None:
        channel = MessagingChannel(workspace_id=project.workspace_id, project_id=project.id,
                                   kind="telegram_user", name=payload.name, config={})
        db.add(channel)
        await db.flush()
    channel.config = {**(channel.config or {}), "phone": payload.phone, "mode": payload.mode}
    try:
        await telegram_profiles.login(db, channel)
    except (ValueError, ChannelError) as error:
        raise HTTPException(422, str(error)) from None
    await db.commit()
    return channel_json(channel)


@router.post("/channels/{channel_id}/telegram-confirm")
async def confirm(channel_id: int, payload: ConfirmIn, request: Request,
                  db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    channel = await managed(db, user, channel_id)
    if channel.status not in {"code_required", "password_required"}:
        raise HTTPException(409, "Сначала запросите код")
    if not payload.code and not payload.password:
        raise HTTPException(422, "Введите код или пароль двухэтапной защиты")
    try:
        await telegram_profiles.login(db, channel, code=payload.code, password=payload.password)
    except (ValueError, ChannelError) as error:
        raise HTTPException(422, str(error)) from None
    if channel.status == "connected":
        channel.active = True
    await db.commit()
    return channel_json(channel)


class ModeIn(BaseModel):
    mode: str = Field(pattern=r"^(corporate|personal)$")


@router.patch("/channels/{channel_id}/telegram-mode")
async def mode(channel_id: int, payload: ModeIn, request: Request,
               db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    channel = await managed(db, user, channel_id)
    channel.config = {**(channel.config or {}), "mode": payload.mode}
    await db.commit()
    return channel_json(channel)
