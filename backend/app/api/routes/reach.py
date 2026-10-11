"""Contact discovery reuses deal access checks and each channel's adapter."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.api.routes.crm import deal_for, project_for
from app.api.routes.messaging import conversation_for
from app.core.access import check_origin, require_portal_user
from app.core.permissions import require_permission
from app.db import get_db
from app.models.crm import CrmContact
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import messaging

router = APIRouter(prefix="/crm", dependencies=[Depends(require_portal_user)])
NO_FIRST = "Через бота и Авито нельзя написать первым. Подключите Telegram-профиль, WhatsApp или MAX"


async def discover(channel, db, contact):
    async def lookup():
        adapter = messaging.adapter(channel, db)
        if not hasattr(adapter, "lookup"):
            return None
        phones = contact.phones or [contact.phone_normalized or None]
        emails = contact.emails or [contact.email_normalized or None]
        for index in range(max(len(phones), len(emails))):
            found = await adapter.lookup(phone=phones[min(index, len(phones) - 1)],
                                         username=contact.telegram,
                                         email=emails[min(index, len(emails) - 1)])
            if found:
                return found
        return None
    return await asyncio.wait_for(lookup(), 10)


@router.post("/deals/{deal_id}/reach/lookup")
async def lookup(deal_id: int, request: Request, db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    contact = await db.get(CrmContact, deal.contact_id)
    channels = (await db.scalars(select(MessagingChannel).where(
        MessagingChannel.workspace_id == deal.workspace_id, MessagingChannel.active.is_(True)))).all()
    accessible = []
    for channel in channels:
        try:
            await project_for(db, user, channel.project_id)
        except HTTPException:
            continue
        accessible.append(channel)

    async def one(channel):
        row = {"channel_id": channel.id, "kind": channel.kind, "name": channel.name,
               "found": False, "title": None, "has_history": False}
        if channel.kind in {"telegram_bot", "avito"}:
            row["reason"] = NO_FIRST
            return row
        try:
            result = await discover(channel, db, contact) if contact else None
            if result:
                row.update(found=True, title=result["title"], has_history=result["has_history"])
        except asyncio.TimeoutError:
            row["error"] = "Канал не ответил за 10 секунд"
        except (messaging.ChannelError, ValueError) as error:
            row["error"] = str(error)[:300]
        return row
    items = await asyncio.gather(*(one(channel) for channel in accessible))
    return {"items": items, "message": NO_FIRST if accessible and all(
        c.kind in {"telegram_bot", "avito"} for c in accessible) else None}


class OpenIn(BaseModel):
    channel_id: int


@router.post("/deals/{deal_id}/reach/open")
async def open_chat(deal_id: int, payload: OpenIn, request: Request,
                    db=Depends(get_db), user=Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    channel = await db.get(MessagingChannel, payload.channel_id)
    if not channel or not channel.active or channel.workspace_id != deal.workspace_id:
        raise HTTPException(404, "Канал не найден")
    await project_for(db, user, channel.project_id)
    if channel.kind in {"telegram_bot", "avito"}:
        raise HTTPException(422, NO_FIRST)
    contact = await db.get(CrmContact, deal.contact_id)
    if not contact:
        raise HTTPException(422, "У сделки нет контакта")
    try:
        found = await discover(channel, db, contact)
        if not found:
            raise HTTPException(404, "Клиент в этом канале не найден")
        history = await asyncio.wait_for(messaging.adapter(channel, db).history(
            found["external_chat_id"], limit=50), 10) if found["has_history"] else []
    except asyncio.TimeoutError:
        raise HTTPException(504, "Канал не ответил за 10 секунд") from None
    except messaging.ChannelError as error:
        raise HTTPException(502, str(error)) from None
    # Serialize competing openings without holding a row lock during network calls.
    await db.scalar(select(MessagingChannel).where(MessagingChannel.id == channel.id).with_for_update())
    row = await db.scalar(select(Conversation).where(Conversation.channel_id == channel.id,
                                                   Conversation.external_chat_id == found["external_chat_id"]))
    if row:
        await conversation_for(db, user, row.id)
        if row.contact_id not in (None, contact.id) or row.deal_id not in (None, deal.id):
            raise HTTPException(409, "Диалог уже связан с другой сделкой")
    else:
        row = Conversation(workspace_id=deal.workspace_id, project_id=channel.project_id,
                           channel_id=channel.id, external_chat_id=found["external_chat_id"],
                           title=contact.name, phone=(contact.phones or [None])[0], unread_count=0)
        db.add(row)
        await db.flush()
    row.contact_id, row.deal_id = contact.id, deal.id
    row.assigned_user_id = deal.responsible_user_id
    for item in history:
        if await db.scalar(select(Message.id).where(Message.conversation_id == row.id,
                                                    Message.external_id == item["external_id"])):
            continue
        db.add(Message(conversation_id=row.id, external_id=item["external_id"], direction=item["direction"],
                       text=item["text"], sent_at=item["sent_at"], status="received" if item["direction"] == "in"
                       else "sent", attachments=[]))
        if not row.last_message_at or messaging.aware(item["sent_at"]) > messaging.aware(row.last_message_at):
            row.last_message_at, row.last_message_preview = item["sent_at"], item["text"][:300]
            row.last_direction = item["direction"]
    await db.commit()
    return {"id": row.id, "conversation_id": row.id}
