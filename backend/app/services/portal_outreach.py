"""Read models for the client portal. No sending or CRM mutations."""
import re

from sqlalchemy import String, case, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen.models import LgCampaign, LgCompany, LgMailbox, LgTouch
from app.models import Campaign, Lead, LeadStatus, TelegramAccount
from app.models.automation import CampaignDialog, CampaignMessage
from app.models.outreach import OutreachThreadState
from app.services import outreach_inbox as inbox


def _email_events(ws: int):
    t, c = LgTouch, LgCompany
    outbound = (t.direction == "out") & (
        t.status.in_(("sent", "delivered", "replied")) | ((t.status == "bounced") & t.external_id.is_not(None)))
    inbound = (t.direction == "in") & inbox._visible_email_reply()
    address = func.lower(func.trim(t.address))
    campaign = (select(LgCampaign.name).where(LgCampaign.workspace_id == ws, LgCampaign.sequence_id == t.campaign_id,
                t.campaign_kind.in_(("sequence", "manual_reply"))).order_by(LgCampaign.id).limit(1).scalar_subquery())
    return (select(literal("email").label("channel"), c.id.label("company_id"),
                   func.coalesce(c.display_name, c.domain).label("company"), address.label("contact"),
                   address.label("person"), campaign.label("campaign"), t.happened_at.label("at"),
                   t.id.label("event_id"), case((outbound, 1), else_=0).label("sent"),
                   case((inbound, 1), else_=0).label("answered"),
                   case((inbound & t.hot_at.is_not(None), 1), else_=0).label("hot"),
                   case((inbound & (t.label == "interested"), 1), else_=0).label("interest"),
                   case((inbound & t.label.in_(("not_interested", "unsubscribe")), 1), else_=0).label("refusal"))
            .select_from(t).join(c, c.id == t.company_id)
            .where(c.workspace_id == ws, t.channel == "email", t.address.contains("@"), or_(outbound, inbound)))


def _telegram_events(ws: int):
    m, d, st = CampaignMessage, CampaignDialog, OutreachThreadState
    interest = or_(Lead.status.in_((LeadStatus.INTERESTED, LeadStatus.HANDOFF)), st.label == "interested")
    refused = or_(Lead.status == LeadStatus.NOT_INTERESTED, st.label.in_(("not_interested", "unsubscribe")))
    username = func.nullif(func.trim(d.username), "")
    return (select(literal("telegram").label("channel"), literal(None).label("company_id"),
                   literal(None).label("company"), (literal("@") + username).label("contact"),
                   cast(d.peer_id, String).label("person"), Campaign.name.label("campaign"),
                   m.created_at.label("at"), m.id.label("event_id"),
                   case((m.status == "sent", 1), else_=0).label("sent"),
                   case((m.status == "received", 1), else_=0).label("answered"),
                   case((or_(st.hot_at.is_not(None), interest), 1), else_=0).label("hot"),
                   case((interest, 1), else_=0).label("interest"), case((refused, 1), else_=0).label("refusal"))
            .select_from(m).join(d, (d.id == m.dialog_id) & (d.campaign_id == m.campaign_id))
            .join(Campaign, Campaign.id == d.campaign_id).join(Lead, (Lead.id == d.lead_id) & (Lead.campaign_id == d.campaign_id))
            .outerjoin(st, (st.workspace_id == ws) & (st.thread == literal("tg:") + cast(d.id, String)))
            .where(inbox._tg_in(Campaign.workspace_id, [ws]), m.status.in_(("sent", "received")),
                   inbox._visible_telegram_message()))


def _contact_rows(ws: int, channel: str | None = None):
    queries = {"email": _email_events, "telegram": _telegram_events}
    events = (queries[channel](ws) if channel else union_all(*[query(ws) for query in queries.values()])).subquery()
    e = events.c
    # Aggregate replies and sends across campaigns, displaying the campaign of the first actual send.
    key = (e.channel, e.company_id, e.person)
    ranked = select(*e, func.row_number().over(
        partition_by=key, order_by=(e.sent.desc(), e.at, e.event_id)).label("first"),
        func.sum(e.sent).over(partition_by=key).label("sent_count"),
        func.max(e.answered).over(partition_by=key).label("replied"),
        func.max(e.hot).over(partition_by=key).label("is_hot"),
        func.max(e.interest).over(partition_by=key).label("is_interested"),
        func.max(e.refusal).over(partition_by=key).label("is_refused"),
    ).subquery()
    return select(ranked).where(ranked.c.first == 1, ranked.c.sent_count > 0).subquery()


async def summary(db: AsyncSession, workspace_id: int) -> dict:
    rows = _contact_rows(workspace_id)
    r = rows.c
    person = r.channel + literal(":") + r.person
    result = (await db.execute(select(
        func.count(func.distinct(r.company_id)).label("companies"),
        func.count(func.distinct(person)).label("people"),
        func.coalesce(func.sum(r.sent_count), 0).label("sent"),
        func.count(func.distinct(case((r.replied == 1, person)))).label("replied"),
        func.count(func.distinct(case(((r.is_hot == 1) & (r.is_refused == 0), person)))).label("hot"),
    ).select_from(rows))).mappings().one()
    return dict(result)


async def contacted(db: AsyncSession, workspace_id: int, *, channel=None, q=None, page=1, per_page=50) -> dict:
    rows = _contact_rows(workspace_id, channel)
    r = rows.c
    query = select(rows)
    if q and q.strip():
        needle = q.strip()
        variants = {needle.casefold(), needle.upper(), needle.title()}
        query = query.where(or_(*(func.lower(column).contains(value, autoescape=True)
                                  for column in (r.company, r.contact, r.campaign) for value in variants)))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = (await db.execute(query.order_by(r.at.desc().nulls_last(), r.channel, r.event_id)
                               .offset((page - 1) * per_page).limit(per_page))).mappings()
    return {"total": total, "items": [{
        "company": item["company"], "contact": item["contact"], "channel": item["channel"],
        "campaign": item["campaign"], "first_contact_at": inbox._iso(item["at"]),
        "status": ("refused" if item["is_refused"] else "interested" if item["is_interested"] or item["is_hot"]
                   else "replied" if item["replied"] else "sent"),
    } for item in result]}


async def visible_threads(db: AsyncSession, ws: int, items: list[dict]) -> list[dict]:
    """Allowlist fields. Resolve the sender anew, without account phones or a cross-tenant sender fallback."""
    box_ids = {item["sender"]["id"] for item in items if item["channel"] == "email"}
    tg_ids = {item["sender"]["id"] for item in items if item["channel"] == "telegram"}
    boxes = dict((await db.execute(select(LgMailbox.id, LgMailbox.email).where(
        LgMailbox.workspace_id == ws, LgMailbox.id.in_(box_ids)))).all()) if box_ids else {}
    accounts = dict((await db.execute(select(TelegramAccount.id, TelegramAccount.username).where(
        inbox._tg_in(TelegramAccount.workspace_id, [ws]), TelegramAccount.id.in_(tg_ids)))).all()) if tg_ids else {}
    visible = []
    for item in items:
        channel = item["channel"]
        ident = item["sender"]["id"]
        username = (accounts.get(ident) or "").lstrip("@") if channel == "telegram" else ""
        sender = boxes.get(ident) if channel == "email" else (
            f"@{username}" if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", username) else None)
        label = item["label"]
        row = {
            "key": item["key"], "channel": channel,
            "campaign": item["campaign"]["name"] if item["campaign"] else None,
            "contact": {key: item["contact"][key] for key in ("name", "address", "company")},
            "sender": sender,
            "last": {key: item["last"][key] for key in ("text", "at", "direction")},
            "label": label if label in ("interested", "question", "later", "not_interested", "unsubscribe") else None,
            "hot": bool(item["hot"]),
        }
        if "messages" in item:
            row["messages"] = [{
                "id": msg["id"], "text": msg["text"], "at": msg["at"], "direction": msg["direction"],
                "author": "Лид" if msg["direction"] == "in" else "Агентство",
            } for msg in item["messages"]]
        visible.append(row)
    return visible
