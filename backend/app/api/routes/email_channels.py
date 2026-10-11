"""Mail credentials are stored using the existing channel encryption."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict

from app.api.routes.crm import project_for
from app.api.routes.messaging import can_manage_channels, channel_json
from app.core.access import check_origin, require_portal_user
from app.core.crypto import encrypt_secret
from app.db import get_db
from app.models.messaging import MessagingChannel
from app.services.email_channel import EmailChannel, PRESETS, valid_email
from app.services.messaging import ChannelError

router = APIRouter(prefix="/crm", dependencies=[Depends(require_portal_user)])


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=180)
    email: str = Field(max_length=254)
    login: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=500)
    imap_host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$", max_length=254)
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$", max_length=254)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    signature: str = Field(default="", max_length=2000)


@router.get("/email-presets")
async def presets():
    return PRESETS


@router.post("/projects/{project_id}/email-channel", status_code=201)
async def create(project_id: int, payload: EmailIn, request: Request,
                 db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настраивать каналы может руководитель или владелец")
    if not valid_email(payload.email) or "\n" in payload.login or "\r" in payload.login:
        raise HTTPException(422, "Укажите корректный email и логин")
    config = payload.model_dump(exclude={"name", "password"})
    config["email"] = payload.email.lower()
    channel = MessagingChannel(workspace_id=project.workspace_id, project_id=project.id,
        kind="email", name=payload.name, secret_encrypted=encrypt_secret(payload.password), config=config)
    db.add(channel)
    try:
        details = await EmailChannel(channel).test()
    except ChannelError as error:
        raise HTTPException(422, str(error)) from None
    channel.config = {**config, "imap_uid": details["imap_uid"]}
    channel.status = "connected"
    await db.commit()
    return channel_json(channel)


@router.post("/channels/{channel_id}/email-check")
async def check(channel_id: int, request: Request, db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    channel = await db.get(MessagingChannel, channel_id)
    if not channel or channel.kind != "email" or channel.workspace_id != user.workspace_id:
        raise HTTPException(404, "Почтовый канал не найден")
    await project_for(db, user, channel.project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Недостаточно прав")
    try:
        await EmailChannel(channel).test()
    except ChannelError as error:
        channel.status, channel.last_error = "error", str(error)
        await db.commit()
        raise HTTPException(422, str(error)) from None
    channel.status, channel.last_error = "connected", None
    await db.commit()
    return {"ok": True}
