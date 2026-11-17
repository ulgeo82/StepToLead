"""Лимит первых касаний в Telegram: у компании (клиента) — по умолчанию на аккаунт в день, у аккаунта — точечно.

Действующий лимит аккаунта = его собственный (если задан) иначе лимит компании. Считается по аккаунту за сутки
по всем кампаниям сразу. Хранение компании — app_settings["telegram:limits:<workspace_id>"].
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import AppSetting
from app.services import tg_space

DEFAULT_FIRST_PER_DAY = 10          # безопасно для прогретых аккаунтов; новым — меньше (прогрев)
MAX_FIRST_PER_DAY = 200


def _key(workspace_id: int | None) -> str:
    return f"telegram:limits:{int(workspace_id or tg_space.home() or 0)}"


async def company(db: AsyncSession, workspace_id: int | None) -> dict:
    row = await db.get(AppSetting, _key(workspace_id))
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    return {"first_per_day": int(value.get("first_per_day", DEFAULT_FIRST_PER_DAY))}


async def save_company(db: AsyncSession, workspace_id: int | None, first_per_day: int) -> dict:
    row = await db.get(AppSetting, _key(workspace_id))
    value = {**(row.value if row and isinstance(row.value, dict) else {}), "first_per_day": int(first_per_day)}
    if row is None:
        db.add(AppSetting(key=_key(workspace_id), value=value))
    else:
        row.value = value
    await db.flush()
    return {"first_per_day": value["first_per_day"]}


async def first_limit(db: AsyncSession, account) -> int:
    if account.daily_first_limit is not None:
        return int(account.daily_first_limit)
    return (await company(db, account.workspace_id))["first_per_day"]
