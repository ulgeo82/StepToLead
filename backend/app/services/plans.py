"""Agency tariffs (audit → pricing): what the client's portal includes on each plan.

The agency sets the plan in the admin (Клиенты). A workspace without a plan has individual terms and no limits,
so clients from before tariffs keep everything they use.
"""
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import ClientWorkspace, PortalUser

FEATURES = {
    "all_chats": "Telegram-бот в чатах",
    "telephony": "Телефония и коллтрекинг",
    "ai_chat": "ИИ-подсказки и резюме в чатах",
    "conversions": "Продажи в Директ и Метрику (ROMI)",
    "ai_calls": "ИИ-разбор звонков",
}
PLANS = {
    "start": {"name": "Старт", "price": 35000, "users": 3, "features": set()},
    "growth": {"name": "Рост", "price": 65000, "users": 7, "features": {"all_chats", "telephony", "ai_chat", "conversions"}},
    "system": {"name": "Система", "price": 110000, "users": None, "features": set(FEATURES)},
}
ORDER = ["start", "growth", "system"]


def plan_info(code: str | None) -> dict:
    if code not in PLANS:
        return {"code": None, "name": "Индивидуальный", "users": None, "features": sorted(FEATURES), "price": None}
    plan = PLANS[code]
    return {"code": code, "name": plan["name"], "users": plan["users"], "features": sorted(plan["features"]), "price": plan["price"]}


def allows(code: str | None, feature: str) -> bool:
    return code not in PLANS or feature in PLANS[code]["features"]


def lowest_with(feature: str) -> str:
    return next(PLANS[c]["name"] for c in ORDER if feature in PLANS[c]["features"])


async def plan_of(db: AsyncSession, workspace_id: int) -> str | None:
    workspace = await db.get(ClientWorkspace, workspace_id)
    return workspace.plan if workspace and workspace.plan in PLANS else None


async def has(db: AsyncSession, workspace_id: int, feature: str) -> bool:
    return allows(await plan_of(db, workspace_id), feature)


async def require(db: AsyncSession, workspace_id: int, feature: str) -> None:
    if not await has(db, workspace_id, feature):
        raise HTTPException(403, f"«{FEATURES[feature]}» входит в тариф «{lowest_with(feature)}». Чтобы подключить, напишите вашему маркетологу StepToLead.")


async def require_seat(db: AsyncSession, workspace_id: int) -> None:
    code = await plan_of(db, workspace_id)
    limit = PLANS[code]["users"] if code else None
    if limit is None:
        return
    active = await db.scalar(select(func.count(PortalUser.id)).where(PortalUser.workspace_id == workspace_id, PortalUser.active.is_(True)))
    if active >= limit:
        raise HTTPException(403, f"В тарифе «{PLANS[code]['name']}» до {limit} сотрудников в портале. Отключите неактивного сотрудника или перейдите на тариф выше.")


async def overview(db: AsyncSession, workspace_id: int) -> dict:
    code = await plan_of(db, workspace_id)
    users = await db.scalar(select(func.count(PortalUser.id)).where(PortalUser.workspace_id == workspace_id, PortalUser.active.is_(True)))
    info = plan_info(code)
    return {**info, "users_used": users, "all_features": [{"key": k, "name": v, "included": allows(code, k),
                                                           "from": lowest_with(k)} for k, v in FEATURES.items()],
            "plans": [plan_info(c) for c in ORDER]}
