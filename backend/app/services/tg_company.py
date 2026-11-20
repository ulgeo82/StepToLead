"""Telegram-аутрич компании (docs/TELEGRAM_COMPANY.md): одна рассылка на компанию, базы — гипотезы.

- Гипотеза — это кампания компании (таблица campaigns) со своей базой получателей и первыми сообщениями из файла.
- Настройки рассылки (режим ответов, ИИ, расписание, паузы, чёрный список) — одни на компанию; при сохранении
  копируются во все её гипотезы. Лимит первых касаний — компании/аккаунта (tg_limits).
- Аккаунты — все рабочие аккаунты компании, во всех её гипотезах.
- Первые сообщения отправляет только ОДНА активная гипотеза (у остальных daily_limit = 0). Остальные гипотезы
  компании продолжают работать, пока в них есть живые диалоги: ответы лидов читаются и обрабатываются.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models import Campaign, Lead, LeadStatus, TelegramAccount
from app.models.automation import CampaignAccount, CampaignDialog, CampaignRuntime
from app.models.system import AppSetting
from app.schemas.automation import AutomationSettings
from app.services import tg_space

DEAD = ("unauthorized", "banned")


def _key(ws: int) -> str:
    return f"telegram:outreach:{int(ws)}"


async def state(db: AsyncSession, ws: int) -> dict:
    row = await db.get(AppSetting, _key(ws))
    return dict(row.value) if row and isinstance(row.value, dict) else {}


async def _save_state(db: AsyncSession, ws: int, value: dict) -> None:
    row = await db.get(AppSetting, _key(ws))
    if row is None:
        db.add(AppSetting(key=_key(ws), value=value))
    else:
        row.value = dict(value)
    await db.flush()


async def campaigns(db: AsyncSession, ws: int) -> list[Campaign]:
    return list((await db.scalars(select(Campaign).where(tg_space.belongs(Campaign.workspace_id, ws))
                                  .order_by(Campaign.id))).all())


async def runtime(db: AsyncSession, campaign_id: int) -> CampaignRuntime:
    row = await db.scalar(select(CampaignRuntime).where(CampaignRuntime.campaign_id == campaign_id))
    if row is None:
        row = CampaignRuntime(campaign_id=campaign_id, status="paused", settings={})
        db.add(row)
        await db.flush()
    return row


async def settings(db: AsyncSession, ws: int) -> dict:
    st = await state(db, ws)
    base = st.get("settings")
    if base is None:   # первый раз — берём настройки самой свежей гипотезы, чтобы ничего не потерять
        last = (await campaigns(db, ws))[-1:] or []
        base = (await runtime(db, last[0].id)).settings if last else {}
    out = AutomationSettings(**(base or {})).model_dump()
    out.pop("daily_limit", None)
    return {"settings": out, "api_key_set": bool(st.get("api_key_encrypted")), "active_id": st.get("active_id"),
            "status": st.get("status", "paused")}


async def save_settings(db: AsyncSession, ws: int, values: dict, api_key: str | None) -> dict:
    values = {k: v for k, v in values.items() if k != "daily_limit"}
    AutomationSettings(**values)   # проверка диапазонов
    st = await state(db, ws)
    st["settings"] = values
    if api_key:
        st["api_key_encrypted"] = encrypt_secret(api_key)
    await _save_state(db, ws, st)
    await apply(db, ws)
    return await settings(db, ws)


async def working_accounts(db: AsyncSession, ws: int) -> list[int]:
    return list((await db.scalars(select(TelegramAccount.id).where(
        tg_space.belongs(TelegramAccount.workspace_id, ws), TelegramAccount.session_encrypted.is_not(None),
        TelegramAccount.proxy_id.is_not(None), TelegramAccount.status.not_in(DEAD)).order_by(TelegramAccount.id))).all())


async def apply(db: AsyncSession, ws: int) -> None:
    """Настройки компании, ключ ИИ и аккаунты — во все гипотезы; первые сообщения — только у активной."""
    st = await state(db, ws)
    base = st.get("settings") or {}
    accounts = await working_accounts(db, ws)
    for c in await campaigns(db, ws):
        rt = await runtime(db, c.id)
        rt.settings = {**AutomationSettings(**base).model_dump(), "daily_limit": 1 if c.id == st.get("active_id") else 0}
        if st.get("api_key_encrypted"):
            rt.api_key_encrypted = st["api_key_encrypted"]
        current = set((await db.scalars(select(CampaignAccount.account_id).where(CampaignAccount.campaign_id == c.id))).all())
        for aid in set(accounts) - current:
            db.add(CampaignAccount(campaign_id=c.id, account_id=aid))
        for aid in current - set(accounts):
            row = await db.get(CampaignAccount, {"campaign_id": c.id, "account_id": aid})
            if row is not None:
                await db.delete(row)
    await db.flush()


async def _has_dialogs(db: AsyncSession, campaign_id: int) -> bool:
    return bool(await db.scalar(select(CampaignDialog.id).where(CampaignDialog.campaign_id == campaign_id).limit(1)))


async def activate(db: AsyncSession, ws: int, campaign_id: int) -> None:
    ids = {c.id for c in await campaigns(db, ws)}
    if campaign_id not in ids:
        raise HTTPException(404, "Гипотеза не найдена")
    st = await state(db, ws)
    st["active_id"] = campaign_id
    await _save_state(db, ws, st)
    await apply(db, ws)


async def deactivate(db: AsyncSession, ws: int) -> None:
    st = await state(db, ws)
    st["active_id"] = None
    await _save_state(db, ws, st)
    await apply(db, ws)


async def set_status(db: AsyncSession, ws: int, running: bool, start_one) -> dict:
    """Запуск/пауза рассылки компании. start_one(campaign_id) — проверки и запуск активной гипотезы."""
    st = await state(db, ws)
    await apply(db, ws)
    if not running:
        for c in await campaigns(db, ws):
            (await runtime(db, c.id)).status = "paused"
        st["status"] = "paused"
        await _save_state(db, ws, st)
        await db.commit()
        return st
    if not await working_accounts(db, ws):
        raise HTTPException(422, "Нет рабочих аккаунтов компании: нужен аккаунт с сессией и прокси")
    active = st.get("active_id")
    await db.commit()
    if active:
        await start_one(active)        # проверит ИИ (если включён), базу и аккаунты
    for c in await campaigns(db, ws):  # остальные гипотезы — только дочитывать ответы
        if c.id != active and await _has_dialogs(db, c.id):
            (await runtime(db, c.id)).status = "running"
    st["status"] = "running"
    await _save_state(db, ws, st)
    await db.commit()
    return st


async def overview(db: AsyncSession, ws: int) -> list[dict]:
    st = await state(db, ws)
    out = []
    for c in await campaigns(db, ws):
        counts = dict((await db.execute(select(Lead.status, func.count()).where(Lead.campaign_id == c.id)
                                        .group_by(Lead.status))).all())
        n = lambda *keys: sum(int(counts.get(k, 0)) for k in keys)   # noqa: E731
        rt = await runtime(db, c.id)
        out.append({"id": c.id, "name": c.name, "description": c.description, "active": c.id == st.get("active_id"),
                    "status": rt.status, "last_error": rt.last_error,
                    "total": n(*LeadStatus), "waiting": n(LeadStatus.NEW, LeadStatus.QUEUED),
                    "sent": n(LeadStatus.SENT, LeadStatus.REPLIED, LeadStatus.INTERESTED, LeadStatus.NOT_INTERESTED, LeadStatus.HANDOFF),
                    "replied": n(LeadStatus.REPLIED, LeadStatus.INTERESTED, LeadStatus.NOT_INTERESTED, LeadStatus.HANDOFF),
                    "interested": n(LeadStatus.INTERESTED, LeadStatus.HANDOFF), "refused": n(LeadStatus.NOT_INTERESTED),
                    "failed": n(LeadStatus.FAILED)})
    return out
