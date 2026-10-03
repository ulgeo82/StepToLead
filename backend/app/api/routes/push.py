"""Web push subscriptions of the current portal user (one per browser / installed app)."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_portal_user
from app.db import get_db
from app.models.marketing import PortalUser
from app.models.system import PushSubscription
from app.services import push

router = APIRouter(prefix="/portal/push", tags=["push"], dependencies=[Depends(require_portal_user)])


@router.get("/key")
async def public_key(db: AsyncSession = Depends(get_db)):
    _, public = await push.vapid_keys(db)
    return {"public_key": public}


class Keys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: str = Field(min_length=20, max_length=1000, pattern=r"^https://")
    keys: Keys


@router.post("/subscribe", status_code=201)
async def subscribe(payload: SubscriptionIn, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    row = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
    if row is None:
        row = PushSubscription(endpoint=payload.endpoint, user_id=user.id, p256dh=payload.keys.p256dh, auth=payload.keys.auth)
        db.add(row)
    row.user_id, row.p256dh, row.auth = user.id, payload.keys.p256dh, payload.keys.auth
    row.user_agent = (request.headers.get("user-agent") or "")[:500]
    await db.commit()
    return {"ok": True}


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)


@router.post("/unsubscribe")
async def unsubscribe(payload: Unsubscribe, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == payload.endpoint,
                                                    PushSubscription.user_id == user.id))
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test(request: Request, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    push.queue(db, user.id, "StepToLead", "Уведомления работают — так будут приходить новые заявки", "/crm")
    push.flush(db)
    return {"ok": True}
