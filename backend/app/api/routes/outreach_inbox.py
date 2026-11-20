"""/api/admin/outreach — единые входящие аутрича (email + Telegram), docs/OUTREACH_INBOX.md, этап C."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.db import get_db
from app.domains.leadgen import space
from app.models.access import AdminUser
from app.models.outreach import OutreachTemplate
from app.services import outreach_inbox as svc

router = APIRouter(prefix="/admin/outreach", tags=["admin-outreach"], dependencies=[Depends(space.bind)])


@router.get("/inbox")
async def inbox_list(channel: Literal["email", "telegram"] | None = None,
                     status: Literal["unhandled", "mine", "hot", "all"] = "unhandled",
                     label: str | None = Query(default=None, max_length=24), q: str | None = Query(default=None, max_length=200),
                     campaign: int | None = None, account: int | None = None,
                     page: int = Query(default=1, ge=1), per_page: int = Query(default=50, ge=1, le=200),
                     all_clients: bool = False,
                     db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    """campaign и account — id в своём канале (кампания лидогена / Telegram, ящик / аккаунт): задавайте вместе с channel."""
    wss = await svc.spaces(db, all_clients=all_clients)
    return await svc.listing(db, wss, admin, status=status, channel=channel, label=label, q=q, campaign=campaign,
                             account=account, page=page, per_page=per_page)


@router.get("/inbox-counter")
async def inbox_counter(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    """Бейдж в меню: без выбранного клиента — по всем клиентам (оператору важно всё, что ждёт ответа)."""
    return await svc.counter(db, await svc.spaces(db, all_clients=True), admin)


async def _conv(db: AsyncSession, key: str) -> svc.Conv:
    return await svc.find(db, key, await svc.item_spaces(db))


@router.get("/inbox/{key}")
async def inbox_item(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    return await svc.detail(db, await _conv(db, key))


@router.post("/inbox/{key}/claim")
async def inbox_claim(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    conv = await _conv(db, key)
    st = await svc.state(db, conv.thread, conv.ws)
    st.assignee_id = admin.id
    await db.commit()
    return await svc.conversation(db, conv)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: int | None = None


@router.post("/inbox/{key}/assign")
async def inbox_assign(key: str, payload: AssignIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    conv = await _conv(db, key)
    if payload.user_id is not None and payload.user_id not in {o["id"] for o in await svc.operators(db)}:
        raise HTTPException(422, "Такого оператора нет")
    st = await svc.state(db, conv.thread, conv.ws)
    st.assignee_id = payload.user_id
    await db.commit()
    return await svc.conversation(db, conv)


@router.post("/inbox/{key}/lock")
async def inbox_lock(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    conv = await _conv(db, key)
    until = await svc.lock(db, await svc.state(db, conv.thread, conv.ws), admin)
    await db.commit()
    return {"until": svc._iso(until)}


@router.delete("/inbox/{key}/lock", status_code=204)
async def inbox_unlock(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    conv = await _conv(db, key)
    st = await svc.state(db, conv.thread, conv.ws, create=False)
    if st is not None:
        svc.unlock(st, admin.id)
        await db.commit()
    return Response(status_code=204)


class ReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)
    template_id: int | None = None


@router.post("/inbox/{key}/reply")
async def inbox_reply(key: str, payload: ReplyIn, db: AsyncSession = Depends(get_db),
                      admin: AdminUser = Depends(require_admin)):
    conv = await _conv(db, key)
    if conv.channel == "telegram" and len(payload.text) > 4096:
        raise HTTPException(422, "В Telegram сообщение — не больше 4096 символов")
    transport = None
    if conv.channel == "email":
        from app.domains.leadgen import routes as leadgen_routes
        transport = leadgen_routes.mail_transport_factory()
    try:
        return await svc.reply(db, conv, admin, payload.text.strip(), transport=transport)
    except svc.ReplyError as exc:
        await db.rollback()
        raise HTTPException(exc.status, str(exc)) from exc


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["interested", "question", "later", "not_interested", "unsubscribe", "auto", "other"] | None = None
    handled: bool | None = None


@router.patch("/inbox/{key}")
async def inbox_update(key: str, payload: PatchIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    return await svc.update(db, await _conv(db, key), label=payload.label, handled=payload.handled)


@router.post("/inbox/{key}/verify")
async def inbox_verify(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    """Проверить в Telegram, ушли ли неподтверждённые сообщения переписки."""
    conv = await _conv(db, key)
    if conv.channel != "telegram":
        raise HTTPException(422, "Проверка доступна для Telegram")
    try:
        result = await svc.verify_telegram(db, conv)
    except svc.ReplyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {**result, "conversation": await svc.detail(db, conv)}


@router.post("/inbox/{key}/suggest")
async def inbox_suggest(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    return {"text": await svc.suggest(db, await _conv(db, key))}


@router.post("/inbox/{key}/to-crm")
async def inbox_to_crm(key: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    return {"deal_id": await svc.to_crm(db, await _conv(db, key))}


@router.get("/operators")
async def operators(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    return await svc.operators(db)


# ---------------------------------------------------------------- шаблоны ответов (по пространству)

class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=10000)
    channel: Literal["any", "email", "telegram"] = "any"


class TemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    text: str | None = Field(default=None, min_length=1, max_length=10000)
    channel: Literal["any", "email", "telegram"] | None = None


async def _template(db: AsyncSession, template_id: int) -> OutreachTemplate:
    row = await db.get(OutreachTemplate, template_id)
    if row is None or row.workspace_id != space.current():
        raise HTTPException(404, "Шаблон не найден")
    return row


@router.get("/reply-templates")
async def templates_list(channel: Literal["email", "telegram"] | None = None, db: AsyncSession = Depends(get_db),
                         admin: AdminUser = Depends(require_admin)):
    query = svc.templates_query(space.current())
    if channel:
        query = query.where(OutreachTemplate.channel.in_(("any", channel)))
    return [svc.template_out(t) for t in (await db.scalars(query.order_by(OutreachTemplate.title))).all()]


@router.post("/reply-templates", status_code=201)
async def templates_create(payload: TemplateIn, db: AsyncSession = Depends(get_db),
                           admin: AdminUser = Depends(require_admin)):
    row = OutreachTemplate(workspace_id=space.current(), title=payload.title.strip(), text=payload.text,
                           channel=payload.channel, created_by_id=admin.id)
    db.add(row)
    await db.commit()
    return svc.template_out(row)


@router.patch("/reply-templates/{template_id}")
async def templates_update(template_id: int, payload: TemplatePatch, db: AsyncSession = Depends(get_db),
                           admin: AdminUser = Depends(require_admin)):
    row = await _template(db, template_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, field, value.strip() if field == "title" else value)
    await db.commit()
    return svc.template_out(row)


@router.delete("/reply-templates/{template_id}", status_code=204)
async def templates_delete(template_id: int, db: AsyncSession = Depends(get_db),
                           admin: AdminUser = Depends(require_admin)):
    await db.delete(await _template(db, template_id))
    await db.commit()
    return Response(status_code=204)
