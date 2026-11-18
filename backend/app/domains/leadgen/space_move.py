"""Перенос аутрича к другому клиенту: завели кампании, ящики и аккаунты у агентства, а это — клиента.

Что переезжает вместе с выбранным:
- email-кампания → её цепочка (если больше никем не используется), записи в цепочку, компании этих записей
  (с контактами, касаниями, письмами — всё привязано к компании), ящики, с которых шли письма кампании
  (если ящик не нужен оставшимся кампаниям), прогревочные письма ящиков, «кто ведёт» во «Входящих»;
- Telegram-кампания → её лиды, очередь, диалоги, сообщения, журнал (всё через кампанию), её аккаунты
  и их прокси (если прокси не нужен оставшимся аккаунтам).
Не переносим, а объясняем: компания участвует в кампании, которая остаётся; аккаунт стоит в кампании, которая
остаётся. Сделки в CRM агентства и расходы на поиск остаются у агентства (это история работы агентства).
Сначала dry_run — что переедет; потом то же без dry_run.
"""
from __future__ import annotations

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen.models import (LgCampaign, LgCompany, LgCompanyKey, LgEnrollment, LgListItem, LgMailbox,
                                        LgSequence, LgWarmupMail)
from app.models import Campaign, Proxy, TelegramAccount
from app.models.automation import CampaignAccount, CampaignDialog
from app.models.outreach import OutreachThreadState
from app.services import tg_space


class MoveError(Exception):
    pass


async def options(db: AsyncSession, ws: int) -> dict:
    lg = (await db.scalars(select(LgCampaign).where(LgCampaign.workspace_id == ws).order_by(LgCampaign.id))).all()
    boxes = (await db.scalars(select(LgMailbox).where(LgMailbox.workspace_id == ws).order_by(LgMailbox.id))).all()
    tg = (await db.scalars(select(Campaign).where(tg_space.belongs(Campaign.workspace_id, ws)).order_by(Campaign.id))).all()
    accs = (await db.scalars(select(TelegramAccount).where(tg_space.belongs(TelegramAccount.workspace_id, ws))
                             .order_by(TelegramAccount.id))).all()
    return {"email_campaigns": [{"id": c.id, "name": c.name, "status": c.status} for c in lg],
            "mailboxes": [{"id": m.id, "email": m.email} for m in boxes],
            "telegram_campaigns": [{"id": c.id, "name": c.name} for c in tg],
            "accounts": [{"id": a.id, "phone": a.phone, "username": a.username} for a in accs]}


async def move(db: AsyncSession, src: int, dst: int, *, email_campaign_ids: list[int], mailbox_ids: list[int],
               telegram_campaign_ids: list[int], account_ids: list[int], dry_run: bool = True) -> dict:
    if src == dst:
        raise MoveError("Это тот же клиент")
    problems: list[str] = []

    # ---- email
    camps = list((await db.scalars(select(LgCampaign).where(LgCampaign.workspace_id == src,
                                                            LgCampaign.id.in_(email_campaign_ids or [0])))).all())
    camp_ids = {c.id for c in camps}
    seq_ids = {c.sequence_id for c in camps}
    enr = list((await db.scalars(select(LgEnrollment).where(LgEnrollment.workspace_id == src, or_(
        LgEnrollment.campaign_id.in_(camp_ids or {0}),
        LgEnrollment.sequence_id.in_(seq_ids or {0}) & LgEnrollment.campaign_id.is_(None))))).all())
    company_ids = {e.company_id for e in enr}
    # компании, которые участвуют и в остающихся кампаниях/цепочках
    shared = set((await db.scalars(select(LgEnrollment.company_id).where(
        LgEnrollment.company_id.in_(company_ids or {0}), LgEnrollment.id.not_in([e.id for e in enr] or [0])))).all())
    if shared:
        problems.append(f"Компаний в других кампаниях агентства: {len(shared)} — выберите и те кампании "
                        f"или уберите компании из них")
    # цепочка переезжает, если её не использует остающаяся кампания
    staying_seq = set((await db.scalars(select(LgCampaign.sequence_id).where(
        LgCampaign.workspace_id == src, LgCampaign.id.not_in(camp_ids or {0}), LgCampaign.sequence_id.in_(seq_ids or {0})))).all())
    if staying_seq:
        problems.append("Цепочку кампании использует другая, остающаяся кампания — перенесите их вместе")
    # ящики: выбранные + с которых писали переносимым компаниям, если не нужны остающимся
    used_boxes = {e.mailbox_id for e in enr if e.mailbox_id}
    want_boxes = set(mailbox_ids or []) | used_boxes
    boxes = list((await db.scalars(select(LgMailbox).where(LgMailbox.workspace_id == src,
                                                           LgMailbox.id.in_(want_boxes or {0})))).all())
    staying_boxes = set((await db.scalars(select(LgEnrollment.mailbox_id).where(
        LgEnrollment.workspace_id == src, LgEnrollment.mailbox_id.in_({b.id for b in boxes} or {0}),
        LgEnrollment.id.not_in([e.id for e in enr] or [0]), LgEnrollment.status.in_(("active", "paused"))))).all())
    if staying_boxes:
        problems.append(f"Ящиков пишут и по остающимся кампаниям: {len(staying_boxes)} — перенесите их кампании тоже")
    box_ids = {b.id for b in boxes}

    # ---- Telegram
    tg_camps = list((await db.scalars(select(Campaign).where(tg_space.belongs(Campaign.workspace_id, src),
                                                             Campaign.id.in_(telegram_campaign_ids or [0])))).all())
    tg_ids = {c.id for c in tg_camps}
    acc_ids = set(account_ids or []) | set((await db.scalars(select(CampaignAccount.account_id).where(
        CampaignAccount.campaign_id.in_(tg_ids or {0})))).all())
    accounts = list((await db.scalars(select(TelegramAccount).where(tg_space.belongs(TelegramAccount.workspace_id, src),
                                                                    TelegramAccount.id.in_(acc_ids or {0})))).all())
    busy = (await db.execute(select(TelegramAccount.phone, Campaign.name)
                             .join(CampaignAccount, CampaignAccount.account_id == TelegramAccount.id)
                             .join(Campaign, Campaign.id == CampaignAccount.campaign_id)
                             .where(TelegramAccount.id.in_({a.id for a in accounts} or {0}),
                                    Campaign.id.not_in(tg_ids or {0})))).all()
    if busy:
        problems.append(f"Аккаунт {busy[0][0]} стоит в кампании «{busy[0][1]}», которая остаётся — выберите и её")
    moving_acc = {a.id for a in accounts}
    proxies = []
    for pid in {a.proxy_id for a in accounts if a.proxy_id}:
        others = await db.scalar(select(TelegramAccount.id).where(TelegramAccount.proxy_id == pid,
                                                                  TelegramAccount.id.not_in(moving_acc)).limit(1))
        proxy = await db.get(Proxy, pid)
        if proxy is not None and proxy.pool == "agency" and others is None:
            proxies.append(proxy)

    summary = {"email_campaigns": len(camps), "sequences": len(seq_ids - staying_seq), "companies": len(company_ids),
               "enrollments": len(enr), "mailboxes": len(boxes), "telegram_campaigns": len(tg_camps),
               "accounts": len(accounts), "proxies": len(proxies), "problems": problems, "dry_run": dry_run}
    if dry_run or problems:
        return summary

    for c in camps:
        c.workspace_id = dst
    for sid in seq_ids:
        seq = await db.get(LgSequence, sid)
        if seq is not None:
            seq.workspace_id = dst
    for e in enr:
        e.workspace_id = dst
    if company_ids:
        await db.execute(update(LgCompany).where(LgCompany.id.in_(company_ids)).values(workspace_id=dst))
        await db.execute(update(LgCompanyKey).where(LgCompanyKey.company_id.in_(company_ids)).values(workspace_id=dst))
        await db.execute(LgListItem.__table__.delete().where(LgListItem.company_id.in_(company_ids)))
        await db.execute(update(OutreachThreadState).where(OutreachThreadState.thread.in_(
            [f"email-co:{i}" for i in company_ids])).values(workspace_id=dst))
    for b in boxes:
        b.workspace_id = dst
    if box_ids:
        await db.execute(update(LgWarmupMail).where(or_(LgWarmupMail.from_mailbox_id.in_(box_ids),
                                                        LgWarmupMail.to_mailbox_id.in_(box_ids)),
                                                    LgWarmupMail.workspace_id == src).values(workspace_id=dst))
    for c in tg_camps:
        c.workspace_id = dst
    for a in accounts:
        a.workspace_id = dst
    for p in proxies:
        p.workspace_id = dst
    if tg_ids:
        dialogs = (await db.scalars(select(CampaignDialog.id).where(CampaignDialog.campaign_id.in_(tg_ids)))).all()
        if dialogs:
            await db.execute(update(OutreachThreadState).where(OutreachThreadState.thread.in_(
                [f"tg:{i}" for i in dialogs])).values(workspace_id=dst))
    await db.commit()
    summary["dry_run"] = False
    return summary
