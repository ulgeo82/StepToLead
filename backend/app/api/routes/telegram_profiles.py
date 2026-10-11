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


class QrIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=180)
    mode: str = Field(default="corporate", pattern=r"^(corporate|personal)$")
    channel_id: int | None = None  # re-login of an existing profile


@router.post("/projects/{project_id}/telegram-profile/qr", status_code=201)
async def start_qr(project_id: int, payload: QrIn, request: Request,
                   db=Depends(get_db), user=Depends(require_portal_user)):
    """Login by QR code: the client scans it in Telegram → Settings → Devices → Link Desktop Device."""
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настраивать каналы может руководитель или владелец")
    from app.services import plans
    await plans.require(db, project.workspace_id, "all_chats")
    if payload.channel_id:
        channel = await managed(db, user, payload.channel_id)
        if channel.project_id != project.id:
            raise HTTPException(404, "Telegram-профиль не найден")
        if channel.active and channel.status == "connected":
            raise HTTPException(409, "Профиль подключён. Чтобы войти заново, сначала выключите канал.")
    else:
        if not payload.name:
            raise HTTPException(422, "Укажите название профиля")
        # Profiles whose QR window was simply closed are left half-created: clean them up.
        for stale in (await db.scalars(select(MessagingChannel).where(
                MessagingChannel.project_id == project.id, MessagingChannel.kind == "telegram_user",
                MessagingChannel.status == "qr_pending", MessagingChannel.secret_encrypted.is_(None)))).all():
            if stale.id not in telegram_profiles.qr_logins:
                await db.delete(stale)
        channel = MessagingChannel(workspace_id=project.workspace_id, project_id=project.id, kind="telegram_user",
                                   name=payload.name.strip(), config={"mode": payload.mode}, active=False,
                                   status="qr_pending")
        db.add(channel)
        await db.flush()
    try:
        qr = await telegram_profiles.start_qr(db, channel)
    except ChannelError as error:
        if not payload.channel_id and not channel.secret_encrypted and channel.status in {"qr_pending", "waiting_proxy"}:
            await db.delete(channel)
        await db.commit()
        raise HTTPException(422, str(error)) from None
    await db.commit()
    return {"channel": channel_json(channel), "qr": qr}


@router.get("/channels/{channel_id}/telegram-qr")
async def qr_status(channel_id: int, db=Depends(get_db), user=Depends(require_portal_user)):
    channel = await managed(db, user, channel_id)
    state = telegram_profiles.qr_state(channel_id)
    if state is None:
        await db.refresh(channel)
        state = {"status": "connected" if channel.status == "connected" else "expired",
                 "error": None if channel.status == "connected" else (channel.last_error or "QR-код устарел — покажите новый")}
    return {**state, "channel": channel_json(channel)}


class QrPasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=300)


@router.post("/channels/{channel_id}/telegram-qr/password")
async def qr_password(channel_id: int, payload: QrPasswordIn, request: Request,
                      db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    await managed(db, user, channel_id)
    try:
        result = await telegram_profiles.qr_password(channel_id, payload.password)
    except ChannelError as error:
        raise HTTPException(422, str(error)) from None
    if result["status"] != "connected":
        raise HTTPException(422, result.get("error") or "Не удалось подключить профиль")
    return result


@router.delete("/channels/{channel_id}/telegram-qr", status_code=204)
async def qr_cancel(channel_id: int, request: Request, db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    channel = await managed(db, user, channel_id)
    await telegram_profiles.cancel_qr(channel_id)
    await db.refresh(channel)
    if not channel.secret_encrypted and channel.status != "connected":
        await db.delete(channel)  # an unfinished new profile leaves nothing behind
    await db.commit()
