"""Перенос Telegram-аккаунтов и прокси аутрича к другому клиенту (у каждой компании — свои аккаунты и прокси).

Аккаунт переезжает вместе со своим прокси, если на прокси нет аккаунтов, которые остаются. Аккаунт, стоящий в
кампании другого пространства, не переносим — сначала уберите его из кампании (иначе кампания клиента A
писала бы с аккаунта клиента B).
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, Proxy, TelegramAccount
from app.models.automation import CampaignAccount
from app.services import tg_space


def outside(column, ws: int):
    """Запись НЕ из пространства ws (NULL — агентство; ~belongs с NULL в SQL даёт NULL, а не true)."""
    from sqlalchemy import and_, or_
    if ws == tg_space.home():
        return and_(column.is_not(None), column != ws)
    return or_(column.is_(None), column != ws)


async def target(db: AsyncSession, workspace_id: int) -> int:
    from app.domains.leadgen import space
    if await space.active_workspace(db, workspace_id) is None:
        raise HTTPException(404, "Клиент не найден или отключён")
    return workspace_id


async def move_accounts(db: AsyncSession, ids: list[int], workspace_id: int) -> dict:
    ws = await target(db, workspace_id)
    accounts = list((await db.scalars(tg_space.scope(select(TelegramAccount).where(TelegramAccount.id.in_(ids)),
                                                     TelegramAccount.workspace_id))).all())
    if not accounts:
        raise HTTPException(404, "Аккаунты не найдены")
    moving = {a.id for a in accounts}
    busy = (await db.execute(select(TelegramAccount.phone, Campaign.name)
                             .join(CampaignAccount, CampaignAccount.account_id == TelegramAccount.id)
                             .join(Campaign, Campaign.id == CampaignAccount.campaign_id)
                             .where(TelegramAccount.id.in_(moving),
                                    outside(Campaign.workspace_id, ws)))).all()
    if busy:
        phone, name = busy[0]
        raise HTTPException(409, f"Аккаунт {phone} стоит в кампании «{name}» — сначала уберите его из кампании")
    proxies = 0
    for proxy_id in {a.proxy_id for a in accounts if a.proxy_id}:
        proxy = await db.get(Proxy, proxy_id)
        others = await db.scalar(select(TelegramAccount.id).where(TelegramAccount.proxy_id == proxy_id,
                                                                  TelegramAccount.id.not_in(moving)).limit(1))
        if proxy is not None and proxy.pool == "agency" and others is None:
            proxy.workspace_id = ws
            proxies += 1
        elif proxy is not None:      # прокси остаётся у прежнего клиента — аккаунт без прокси, раздадим свободный
            for a in accounts:
                if a.proxy_id == proxy_id:
                    a.proxy_id, a.status = None, "waiting_proxy"
    for a in accounts:
        a.workspace_id = ws
    await db.commit()
    return {"accounts": len(accounts), "proxies": proxies,
            "without_proxy": sum(1 for a in accounts if a.proxy_id is None)}


async def move_proxies(db: AsyncSession, ids: list[int], workspace_id: int) -> dict:
    ws = await target(db, workspace_id)
    query = select(Proxy).where(Proxy.id.in_(ids), Proxy.pool == "agency",
                                tg_space.belongs(Proxy.workspace_id, tg_space.current()))
    proxies = list((await db.scalars(query)).all())
    if not proxies:
        raise HTTPException(404, "Прокси не найдены")
    used = await db.scalar(select(TelegramAccount.phone).where(
        TelegramAccount.proxy_id.in_([p.id for p in proxies]),
        outside(TelegramAccount.workspace_id, ws)).limit(1))
    if used:
        raise HTTPException(409, f"Прокси занят аккаунтом {used} — перенесите аккаунт (прокси переедет с ним) "
                                 "или освободите прокси")
    for p in proxies:
        p.workspace_id = ws
    await db.commit()
    return {"proxies": len(proxies)}
