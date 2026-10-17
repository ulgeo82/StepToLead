"""Токены ИИ: бар остатка и покупка у клиента, лимиты и заявки на оплату — у агентства."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_admin, require_portal_user
from app.db import get_db
from app.models.ai import AiTokenAccount, AiTokenPurchase
from app.models.marketing import ClientWorkspace, PortalUser
from app.services import ai_tokens

router = APIRouter(prefix="/ai-tokens", tags=["ai-tokens"], dependencies=[Depends(require_portal_user)])
public_router = APIRouter(prefix="/public", tags=["ai-tokens-public"])
admin_router = APIRouter(prefix="/portal/admin/ai-tokens", tags=["ai-tokens-admin"], dependencies=[Depends(require_admin)])


def can_buy(user: PortalUser) -> bool:
    return user.role == "client_owner"


async def overview(db, user):
    cfg = await ai_tokens.config(db)
    state = ai_tokens.public_view(await ai_tokens.status(db, user.workspace_id))
    rows = (await db.scalars(select(AiTokenPurchase).where(AiTokenPurchase.workspace_id == user.workspace_id)
                             .order_by(AiTokenPurchase.id.desc()).limit(10))).all()
    return {**state, "packs": cfg["packs"], "can_buy": can_buy(user) and state["ai_included"],
            "online_payment": ai_tokens.yookassa_configured(), "purchases": [ai_tokens.purchase_json(r) for r in rows]}


@router.get("")
async def my_tokens(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    return await overview(db, user)


class BuyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack: int = Field(ge=0, le=20)


@router.post("/purchase")
async def buy(payload: BuyIn, request: Request, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if not can_buy(user):
        raise HTTPException(403, "Покупать токены может владелец аккаунта")
    state = await ai_tokens.status(db, user.workspace_id)
    if not state["ai_included"]:
        raise HTTPException(403, "ИИ-функции не входят в ваш тариф — напишите вашему маркетологу StepToLead")
    packs = (await ai_tokens.config(db))["packs"]
    if payload.pack >= len(packs):
        raise HTTPException(422, "Такого пакета нет")
    pack = packs[payload.pack]
    online = ai_tokens.yookassa_configured()
    row = AiTokenPurchase(workspace_id=user.workspace_id, user_id=user.id, tokens=pack["tokens"], price_rub=pack["price"],
                          status="pending", provider="yookassa" if online else "invoice")
    db.add(row); await db.flush()
    url = None
    if online:
        workspace = await db.get(ClientWorkspace, user.workspace_id)
        try:
            url = await ai_tokens.create_payment(row, workspace.name if workspace else "")
        except (RuntimeError, KeyError, ValueError) as exc:
            await db.rollback()
            raise HTTPException(502, str(exc) if isinstance(exc, RuntimeError) else "Платёжный сервис ответил с ошибкой")
    await db.commit()
    return {"purchase": ai_tokens.purchase_json(row), "payment_url": url,
            "message": None if url else "Заявка отправлена — счёт пришлёт ваш маркетолог StepToLead, токены зачислятся после оплаты."}


@router.post("/purchase/{purchase_id}/check")
async def check(purchase_id: int, request: Request, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    row = await db.get(AiTokenPurchase, purchase_id)
    if row is None or row.workspace_id != user.workspace_id:
        raise HTTPException(404, "Покупка не найдена")
    try:
        await ai_tokens.sync_payment(db, row)
    except Exception:  # сеть/ЮKassa недоступна — статус проверим позже по вебхуку
        pass
    await db.commit()
    return await overview(db, user)


@public_router.post("/yookassa")
async def yookassa_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Уведомление ЮKassa. Телу не доверяем: статус всегда перепроверяем запросом к API."""
    try:
        data = await request.json()
        external = str((data.get("object") or {}).get("id") or "")[:64]
    except (ValueError, AttributeError):
        return {"ok": True}
    if external:
        row = await db.scalar(select(AiTokenPurchase).where(AiTokenPurchase.external_id == external))
        if row is not None:
            try:
                await ai_tokens.sync_payment(db, row)
                await db.commit()
            except Exception:
                await db.rollback()
                raise HTTPException(503, "retry")
    return {"ok": True}


# --------------------------------------------------------------------------- агентство

@admin_router.get("")
async def admin_overview(db: AsyncSession = Depends(get_db)):
    workspaces = (await db.scalars(select(ClientWorkspace).where(ClientWorkspace.status != "deleted").order_by(ClientWorkspace.id))).all()
    pending = (await db.scalars(select(AiTokenPurchase).where(AiTokenPurchase.status == "pending").order_by(AiTokenPurchase.id.desc()))).all()
    names = {w.id: w.name for w in workspaces}
    return {"config": await ai_tokens.config(db), "online_payment": ai_tokens.yookassa_configured(),
            "pending": [{**ai_tokens.purchase_json(p), "workspace_id": p.workspace_id, "workspace": names.get(p.workspace_id, "—")} for p in pending]}


@admin_router.get("/workspaces/{workspace_id}")
async def admin_workspace(workspace_id: int, db: AsyncSession = Depends(get_db)):
    if await db.get(ClientWorkspace, workspace_id) is None:
        raise HTTPException(404, "Клиент не найден")
    account = await db.get(AiTokenAccount, workspace_id)
    rows = (await db.scalars(select(AiTokenPurchase).where(AiTokenPurchase.workspace_id == workspace_id)
                             .order_by(AiTokenPurchase.id.desc()).limit(20))).all()
    return {**await ai_tokens.status(db, workspace_id),
            "monthly_override": account.monthly_override if account else None,
            "reserve_override": account.reserve_override if account else None,
            "purchases": [ai_tokens.purchase_json(r) for r in rows]}


class LimitsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_override: int | None = Field(default=None, ge=0, le=10**10)
    reserve_override: int | None = Field(default=None, ge=0, le=10**10)


@admin_router.put("/workspaces/{workspace_id}")
async def admin_limits(workspace_id: int, payload: LimitsIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if await db.get(ClientWorkspace, workspace_id) is None:
        raise HTTPException(404, "Клиент не найден")
    account = await db.get(AiTokenAccount, workspace_id)
    if account is None:
        account = AiTokenAccount(workspace_id=workspace_id, bought_total=0, bought_spent=0, overflow_recorded=0)
        db.add(account)
    account.monthly_override, account.reserve_override = payload.monthly_override, payload.reserve_override
    await db.commit()
    return await admin_workspace(workspace_id, db)


class CreditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tokens: int = Field(gt=0, le=10**10)
    comment: str | None = Field(default=None, max_length=240)


@admin_router.post("/workspaces/{workspace_id}/credit")
async def admin_credit(workspace_id: int, payload: CreditIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if await db.get(ClientWorkspace, workspace_id) is None:
        raise HTTPException(404, "Клиент не найден")
    row = AiTokenPurchase(workspace_id=workspace_id, tokens=payload.tokens, price_rub=0, status="pending",
                          provider="manual", comment=payload.comment or "Начислено агентством")
    db.add(row); await db.flush()
    await ai_tokens.credit(db, row)
    await db.commit()
    return await admin_workspace(workspace_id, db)


@admin_router.post("/purchases/{purchase_id}/{action}")
async def admin_purchase(purchase_id: int, action: str, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(AiTokenPurchase, purchase_id)
    if row is None or action not in {"paid", "cancel"}:
        raise HTTPException(404, "Заявка не найдена")
    if row.status != "pending":
        raise HTTPException(409, "Заявка уже закрыта")
    if action == "paid":
        await ai_tokens.credit(db, row)
        await ai_tokens.notify_owners(db, row.workspace_id, "Токены ИИ зачислены", f"+{ai_tokens.fmt(row.tokens)} токенов. Спасибо за оплату!")
    else:
        row.status = "canceled"
    await db.commit()
    return await admin_overview(db)


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly: dict[str, int] = Field(default_factory=dict)
    reserve: dict[str, int] = Field(default_factory=dict)
    packs: list[dict[str, int]] = Field(default_factory=list, max_length=6)
    warn_share: float | None = Field(default=None, gt=0, lt=1)


@admin_router.put("/config")
async def admin_config(payload: ConfigIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    allowed = {"start", "growth", "system", "trial", "individual"}
    if any(k not in allowed or v < 0 for k, v in {**payload.monthly, **payload.reserve}.items()):
        raise HTTPException(422, "Неизвестный тариф или отрицательное значение")
    if any(set(p) != {"tokens", "price"} or p["tokens"] <= 0 or p["price"] <= 0 for p in payload.packs):
        raise HTTPException(422, "Пакет: количество токенов и цена больше нуля")
    value = payload.model_dump(exclude_none=True)
    out = await ai_tokens.save_config(db, value)
    await db.commit()
    return {"config": out}
