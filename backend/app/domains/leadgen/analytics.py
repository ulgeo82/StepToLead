"""Аналитика аутрича: воронка компаний, разрез по нишам, цепочки (ответы по шагам), здоровье ящиков."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.models import LgCompany, LgContact, LgEnrichment, LgEnrollment, LgMailbox, LgSequence, LgTouch

FUNNEL = ("found", "with_contacts", "in_outreach", "written", "replied", "converted")
FUNNEL_LABELS = {"found": "Найдено", "with_contacts": "Есть контакты", "in_outreach": "В аутриче",
                 "written": "Написали", "replied": "Ответили", "converted": "Клиенты"}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


async def funnel(db: AsyncSession, workspace_id: int, since: datetime, niche_key: str | None = None) -> list[dict]:
    base = [LgCompany.workspace_id == workspace_id, LgCompany.first_seen_at >= since]
    if niche_key:
        base.append(LgCompany.niche_key == niche_key)
    ids = select(LgCompany.id).where(*base)

    async def count(*extra) -> int:
        return await db.scalar(select(func.count(distinct(LgCompany.id))).where(*base, *extra)) or 0

    with_contacts = await db.scalar(select(func.count(distinct(LgContact.company_id))).where(
        LgContact.company_id.in_(ids), LgContact.is_personal.is_(False))) or 0
    written = await db.scalar(select(func.count(distinct(LgTouch.company_id))).where(
        LgTouch.company_id.in_(ids), LgTouch.direction == "out", LgTouch.status.in_(("sent", "delivered", "replied")))) or 0
    replied = await db.scalar(select(func.count(distinct(LgTouch.company_id))).where(
        LgTouch.company_id.in_(ids), LgTouch.direction == "in", LgTouch.status == "replied")) or 0
    values = {"found": await count(), "with_contacts": with_contacts,
              "in_outreach": await count(LgCompany.crm_deal_id.is_not(None)), "written": written,
              "replied": replied, "converted": await count(LgCompany.stage == "converted")}
    out, prev = [], None
    for key in FUNNEL:
        out.append({"key": key, "label": FUNNEL_LABELS[key], "count": values[key],
                    "from_previous": _rate(values[key], prev) if prev is not None else None})
        prev = values[key]
    return out


async def by_niche(db: AsyncSession, workspace_id: int, since: datetime) -> list[dict]:
    rows = (await db.execute(select(LgCompany.niche, func.count(LgCompany.id),
                                    func.sum(case((LgCompany.crm_deal_id.is_not(None), 1), else_=0)),
                                    func.avg(LgCompany.score))
                             .where(LgCompany.workspace_id == workspace_id, LgCompany.first_seen_at >= since)
                             .group_by(LgCompany.niche).order_by(func.count(LgCompany.id).desc()).limit(30))).all()
    out = []
    for niche, total, in_outreach, avg_score in rows:
        replied = await db.scalar(select(func.count(distinct(LgTouch.company_id))).join(
            LgCompany, LgCompany.id == LgTouch.company_id).where(
            LgCompany.workspace_id == workspace_id, LgCompany.first_seen_at >= since,
            (LgCompany.niche == niche) if niche is not None else LgCompany.niche.is_(None),
            LgTouch.direction == "in", LgTouch.status == "replied")) or 0
        out.append({"niche": niche or "без ниши", "companies": total, "in_outreach": int(in_outreach or 0),
                    "replied": replied, "reply_rate": _rate(replied, int(in_outreach or 0)),
                    "avg_score": round(float(avg_score or 0), 1)})
    return out


async def sequences_report(db: AsyncSession, workspace_id: int, since: datetime) -> list[dict]:
    seqs = (await db.execute(select(LgSequence).where(LgSequence.workspace_id == workspace_id)
                             .order_by(LgSequence.id.desc()))).scalars().all()
    out = []
    for seq in seqs:
        enr_ids = select(LgEnrollment.id).where(LgEnrollment.sequence_id == seq.id, LgEnrollment.started_at >= since)
        statuses = dict((await db.execute(select(LgEnrollment.status, func.count(LgEnrollment.id)).where(
            LgEnrollment.id.in_(enr_ids)).group_by(LgEnrollment.status))).all())
        keys = [f"enr{i}" for (i,) in (await db.execute(enr_ids)).all()]
        touches = (await db.execute(select(LgTouch).where(LgTouch.sequence_id.in_(keys), LgTouch.channel == "email")
                                    .order_by(LgTouch.happened_at, LgTouch.id))).scalars().all() if keys else []
        # Номер письма в цепочке для каждого исходящего и к какому письму относится ответ.
        per_step: dict[int, dict] = {}
        counter: dict[str, int] = {}
        last_step: dict[str, int] = {}
        for t in touches:
            if t.direction == "out" and t.campaign_kind == "manual_reply":
                continue  # ручной ответ из «Входящих» — не письмо цепочки
            if t.direction == "out" and t.status in ("sent", "delivered", "replied", "bounced"):
                counter[t.sequence_id] = counter.get(t.sequence_id, 0) + 1
                n = counter[t.sequence_id]
                last_step[t.sequence_id] = n
                step = per_step.setdefault(n, {"email": n, "sent": 0, "replies": 0, "bounced": 0})
                step["sent" if t.status != "bounced" else "bounced"] += 1
            elif t.direction == "in" and t.status == "replied" and t.sequence_id in last_step:
                per_step[last_step[t.sequence_id]]["replies"] += 1
        steps = [{**v, "reply_rate": _rate(v["replies"], v["sent"])} for _, v in sorted(per_step.items())]
        enrolled = sum(statuses.values())
        replied = statuses.get("replied", 0)
        out.append({"id": seq.id, "name": seq.name, "is_active": seq.is_active, "enrolled": enrolled,
                    "statuses": statuses, "replied": replied, "reply_rate": _rate(replied, enrolled),
                    "unsubscribed": await db.scalar(select(func.count(LgEnrollment.id)).where(
                        LgEnrollment.id.in_(enr_ids), LgEnrollment.stop_reason == "unsubscribed")) or 0,
                    "emails": steps})
    return out


async def mailboxes_report(db: AsyncSession, workspace_id: int, now: datetime) -> list[dict]:
    week = now - timedelta(days=7)
    boxes = (await db.execute(select(LgMailbox).where(LgMailbox.workspace_id == workspace_id)
                              .order_by(LgMailbox.id))).scalars().all()
    out = []
    for mb in boxes:
        rows = dict((await db.execute(select(LgTouch.status, func.count(LgTouch.id)).where(
            LgTouch.mailbox_id == mb.id, LgTouch.happened_at >= week, LgTouch.channel == "email",
            LgTouch.direction == "out").group_by(LgTouch.status))).all())
        replies = await db.scalar(select(func.count(LgTouch.id)).where(
            LgTouch.mailbox_id == mb.id, LgTouch.happened_at >= week, LgTouch.direction == "in",
            LgTouch.status == "replied")) or 0
        sent = sum(v for k, v in rows.items() if k in ("sent", "delivered", "replied"))
        bounced = rows.get("bounced", 0)
        bounce_rate = _rate(bounced, sent + bounced)
        out.append({"id": mb.id, "email": mb.email, "is_active": mb.is_active, "sent_7d": sent, "bounced_7d": bounced,
                    "bounce_rate": bounce_rate, "replies_7d": replies, "reply_rate": _rate(replies, sent),
                    # Больше 5% возвратов — почтовики начинают резать доставку: пора чистить базу.
                    "warning": "Много возвратов — проверьте качество адресов" if (bounce_rate or 0) > 5 else None,
                    "last_error": mb.last_error})
    return out


async def overview(db: AsyncSession, workspace_id: int, *, days: int = 30, niche: str | None = None,
                   now: datetime | None = None) -> dict:
    from app.domains.leadgen.core.normalize import text_key
    now = now or service.utcnow()
    since = now - timedelta(days=days)
    enriched = await db.scalar(select(func.count(distinct(LgEnrichment.company_id))).join(
        LgCompany, LgCompany.id == LgEnrichment.company_id).where(
        LgCompany.workspace_id == workspace_id, LgCompany.first_seen_at >= since,
        and_(LgEnrichment.step == "site_check", LgEnrichment.status == "done"))) or 0
    return {"days": days, "niche": niche, "funnel": await funnel(db, workspace_id, since, text_key(niche)),
            "enriched": enriched, "niches": await by_niche(db, workspace_id, since),
            "sequences": await sequences_report(db, workspace_id, since),
            "mailboxes": await mailboxes_report(db, workspace_id, now)}
