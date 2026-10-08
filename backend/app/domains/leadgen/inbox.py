"""Входящие: ответы на письма аутрича со всех ящиков в одной ленте.

Каждый ответ — это LgTouch(direction="in"). Метка (label) ставится сразу правилами, потом её уточняет ИИ
(если подключён), а последнее слово за человеком. Последствия (отказ → стоп-лист и «Отказ» в CRM,
«интересно» → задача «Связаться») срабатывают только при метке, поставленной человеком: ИИ может ошибиться.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.domains.leadgen import outreach, service
from app.domains.leadgen.models import LgCompany, LgContact, LgDnc, LgEnrollment, LgMailbox, LgSequence, LgTouch
from app.models.crm import CrmDeal, CrmTask
from app.services import ai

LABELS = ("interested", "question", "later", "not_interested", "unsubscribe", "auto", "other")
LABEL_NAMES = {"interested": "Интересно", "question": "Вопрос", "later": "Позже", "not_interested": "Не интересно",
               "unsubscribe": "Отписка", "auto": "Автоответ", "other": "Другое"}
# Метки, по которым ничего делать не нужно: такие ответы сразу считаются разобранными.
SELF_HANDLED = {"unsubscribe", "auto"}
LATER_DEFAULT_DAYS = 30
FEATURE = "summary"

_NOT = re.compile(r"^\W*нет\b|неинтересн|неактуальн|не\s+(интересн|актуальн|нужн|надо|требуется|рассматрива)|нам\s+это\s+не|откаж|отказ|"
                  r"уже\s+(есть|работаем)\s+(с\s+)?(агентств|подрядчик|маркетолог)|не\s+пишите", re.I)
_LATER = re.compile(r"\bпозже\b|\bпозднее\b|через\s+(месяц|пару|полгода|\d+)|после\s+(праздник|нового|сезона)|"
                    r"не\s+сейчас|сейчас\s+не\s+(время|до)|вернитесь|напишите\s+(в|через|позже)", re.I)
_YES = re.compile(r"интересн|давайте|созвон|позвоните|перезвоните|расскажите|подробнее|пришлите|"
                  r"сколько\s+стоит|какая\s+цена|стоимост|присылайте|можно\s+попробовать|хотим|актуальн", re.I)


def rule_label(text: str) -> str:
    """Грубая разметка без ИИ. Порядок важен: «не интересно» содержит «интересн»."""
    t = (text or "").strip()
    if _NOT.search(t):
        return "not_interested"
    if _LATER.search(t):
        return "later"
    if _YES.search(t):
        return "interested"
    if "?" in t:
        return "question"
    return "other"


def initial_label(kind: str, fresh: str) -> tuple[str, str]:
    """kind из core.classify_inbound -> (label, source)."""
    if kind == "auto_reply":
        return "auto", "rule"
    if kind == "unsubscribe":
        return "unsubscribe", "rule"
    return rule_label(fresh), "rule"


# ---------------------------------------------------------------- ИИ-разметка

SYSTEM = (
    "Ты разбираешь ответы компаний на холодное письмо маркетингового агентства. "
    "Определи намерение и ответь ТОЛЬКО JSON без пояснений вокруг:\n"
    '{"label": "interested" | "question" | "later" | "not_interested" | "unsubscribe" | "auto" | "other", '
    '"summary": "суть ответа по-русски, до 120 символов"}\n'
    "interested — готовы обсуждать или просят подробности/цену/созвон; question — задают вопрос, но интереса не "
    "выразили; later — просят вернуться позже; not_interested — вежливый отказ; unsubscribe — просят больше не "
    "писать; auto — автоответ или отпуск; other — всё остальное. Не выдумывай того, чего нет в тексте."
)
_JSON = re.compile(r"\{.*\}", re.S)


class LabelParseError(ValueError):
    pass


def parse_answer(text: str) -> dict:
    m = _JSON.search(text or "")
    if not m:
        raise LabelParseError("нет JSON в ответе")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise LabelParseError("битый JSON") from exc
    label = str(data.get("label", "")).strip().lower()
    if label not in LABELS:
        raise LabelParseError(f"неизвестная метка {label!r}")
    summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()[:300] or None
    return {"label": label, "summary": summary}


def _validator(text: str) -> None:
    try:
        parse_answer(text)
    except LabelParseError as exc:
        raise ai.AIError(f"ИИ вернул ответ не по формату: {exc}") from exc


def available() -> bool:
    return ai.configured(FEATURE)


async def classify(db: AsyncSession, touch: LgTouch, *, complete=None) -> dict:
    """Уточняет метку ответа через ИИ. Метку, поставленную человеком, не трогает."""
    if touch.label_source == "user":
        return {"status": "skipped", "label": touch.label}
    company = await db.get(LgCompany, touch.company_id)
    prompt = (f"Компания: {company.display_name if company else '—'}\n"
              f"Тема: {touch.subject or '—'}\n\nОтвет:\n{(touch.body or '')[:3000]}")
    call = complete or ai.complete
    try:
        text = await call(SYSTEM, [{"role": "user", "content": prompt}], max_tokens=150, temperature=0.1,
                          feature=FEATURE, workspace_id=company.workspace_id if company else None,
                          validator=_validator)
        result = parse_answer(text)
    except (ai.AIError, LabelParseError) as exc:
        return {"status": "failed", "error": str(exc)}
    touch.label, touch.label_source, touch.summary = result["label"], "ai", result["summary"]
    if result["label"] in SELF_HANDLED and touch.handled_at is None:
        touch.handled_at = service.utcnow()
    await db.flush()
    return {"status": "done", **result}


async def classify_pending(db: AsyncSession, *, limit: int = 20, complete=None) -> int:
    """Для воркера: ответы, размеченные только правилами, — через ИИ."""
    rows = (await db.execute(select(LgTouch).where(
        LgTouch.direction == "in", LgTouch.channel == "email", LgTouch.label_source == "rule",
        LgTouch.handled_at.is_(None)).order_by(LgTouch.id).limit(limit))).scalars().all()
    done = 0
    for touch in rows:
        if (await classify(db, touch, complete=complete))["status"] == "done":
            done += 1
        else:
            touch.label_source = "rule_only"  # не дёргаем ИИ по этому письму снова
    await db.flush()
    return done


# ---------------------------------------------------------------- лента

def _inbound(workspace_id: int):
    return (select(LgTouch, LgCompany).join(LgCompany, LgCompany.id == LgTouch.company_id)
            .where(LgCompany.workspace_id == workspace_id, LgTouch.direction == "in", LgTouch.channel == "email"))


async def touch_or_none(db: AsyncSession, workspace_id: int, touch_id: int) -> tuple[LgTouch, LgCompany] | None:
    row = (await db.execute(_inbound(workspace_id).where(LgTouch.id == touch_id))).first()
    return (row[0], row[1]) if row else None


async def row_out(db: AsyncSession, touch: LgTouch, company: LgCompany, *, full: bool = False) -> dict:
    mailbox = await db.get(LgMailbox, touch.mailbox_id) if touch.mailbox_id else None
    sequence = await db.get(LgSequence, touch.campaign_id) if touch.campaign_kind == "sequence" and touch.campaign_id else None
    body = touch.body or ""
    out = {"id": touch.id, "company_id": company.id, "company": company.display_name or company.domain,
           "domain": company.domain, "stage": company.stage, "score": company.score, "fit_label": company.fit_label,
           "crm_deal_id": company.crm_deal_id, "address": touch.address, "mailbox_id": touch.mailbox_id,
           "mailbox": mailbox.email if mailbox else None, "sequence": sequence.name if sequence else None,
           "subject": touch.subject, "snippet": re.sub(r"\s+", " ", body)[:240], "label": touch.label,
           "label_name": LABEL_NAMES.get(touch.label or "", None), "label_source": touch.label_source,
           "summary": touch.summary, "handled": touch.handled_at is not None,
           "handled_at": _iso(touch.handled_at), "happened_at": _iso(touch.happened_at)}
    if full:
        out["body"] = body
        q = select(LgTouch).where(LgTouch.company_id == company.id)
        q = q.where(LgTouch.sequence_id == touch.sequence_id) if touch.sequence_id else q
        thread = (await db.execute(q.order_by(LgTouch.happened_at, LgTouch.id).limit(50))).scalars().all()
        out["thread"] = [{"id": t.id, "direction": t.direction, "channel": t.channel, "status": t.status,
                          "subject": t.subject, "body": t.body, "address": t.address, "label": t.label,
                          "manual": t.campaign_kind == "manual_reply", "happened_at": _iso(t.happened_at)}
                         for t in thread]
        out["can_reply"] = (await reply_blocker(db, touch, company)) is None
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def listing(db: AsyncSession, workspace_id: int, *, status: str = "unhandled", label: str | None = None,
                  mailbox_id: int | None = None, q: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    base = _inbound(workspace_id)
    if status == "unhandled":
        base = base.where(LgTouch.handled_at.is_(None))
    elif status == "handled":
        base = base.where(LgTouch.handled_at.is_not(None))
    if mailbox_id:
        base = base.where(LgTouch.mailbox_id == mailbox_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        variants = {like, like.lower(), like.capitalize()}
        base = base.where(or_(*[c.like(v) for v in variants for c in (
            LgCompany.display_name, LgCompany.domain, LgTouch.subject, LgTouch.body, LgTouch.address)]))
    # Счётчики по меткам — до фильтра по метке, чтобы вкладки показывали, сколько где.
    sub = base.with_only_columns(LgTouch.label).subquery()
    counts = {k or "none": v for k, v in (await db.execute(
        select(sub.c.label, func.count()).group_by(sub.c.label))).all()}
    if label:
        base = base.where(LgTouch.label == label)
    total = await db.scalar(select(func.count()).select_from(base.with_only_columns(LgTouch.id).subquery())) or 0
    rows = (await db.execute(base.order_by(LgTouch.happened_at.desc(), LgTouch.id.desc())
                             .limit(limit).offset(offset))).all()
    unhandled = await db.scalar(select(func.count()).select_from(
        _inbound(workspace_id).where(LgTouch.handled_at.is_(None)).with_only_columns(LgTouch.id).subquery())) or 0
    return {"total": total, "unhandled": unhandled, "counts": counts,
            "labels": [{"key": k, "name": LABEL_NAMES[k]} for k in LABELS],
            "items": [await row_out(db, t, c) for t, c in rows]}


# ---------------------------------------------------------------- решение по ответу

async def _task(db: AsyncSession, company: LgCompany, type_code: str, title: str, description: str | None,
                due: datetime) -> int | None:
    deal = await db.get(CrmDeal, company.crm_deal_id) if company.crm_deal_id else None
    if deal is None:
        return None
    task = CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                   contact_id=deal.contact_id, type_code=type_code, title=title[:220], description=description,
                   due_at=due, priority="HIGH" if type_code == "CALL" else "NORMAL", status="OPEN")
    db.add(task)
    await db.flush()
    return task.id


async def _add_dnc(db: AsyncSession, workspace_id: int, kind: str, value: str, reason: str,
                   until: datetime | None, note: str) -> None:
    row = await db.scalar(select(LgDnc).where(LgDnc.workspace_id == workspace_id, LgDnc.kind == kind,
                                              LgDnc.value_norm == value))
    if row is None:
        db.add(LgDnc(workspace_id=workspace_id, kind=kind, value_norm=value, reason=reason, until=until, note=note))


async def _stop_enrollments(db: AsyncSession, company: LgCompany, reason: str, now: datetime) -> None:
    from app.domains.leadgen.sequences import _finish
    for enr in (await db.execute(select(LgEnrollment).where(
            LgEnrollment.company_id == company.id, LgEnrollment.status.in_(("active", "paused"))))).scalars():
        _finish(enr, "stopped", reason, now)


@dataclass
class Applied:
    task_id: int | None = None
    effect: str | None = None


async def apply_label(db: AsyncSession, touch: LgTouch, company: LgCompany, label: str, *,
                      remind_days: int | None = None, now: datetime | None = None) -> Applied:
    """Метка от человека + её последствия. Повторная та же метка последствий не повторяет."""
    from app.domains.leadgen.sequences import STAGE_LOST, _move_deal
    now = now or service.utcnow()
    same = touch.label == label and touch.label_source == "user"
    touch.label, touch.label_source = label, "user"
    touch.handled_at = touch.handled_at or now
    if same:
        await db.flush()
        return Applied()
    excerpt = (touch.body or "")[:500]
    if label == "interested":
        task_id = await _task(db, company, "CALL", f"Связаться: {company.display_name or company.domain} — интересно",
                              f"Ответ от {touch.address or 'компании'}:\n{excerpt}", now)
        return Applied(task_id, "task:call")
    if label == "later":
        days = remind_days or LATER_DEFAULT_DAYS
        await _stop_enrollments(db, company, "later", now)
        task_id = await _task(db, company, "MESSAGE", f"Вернуться: {company.display_name or company.domain}",
                              f"Просили написать позже. Их ответ:\n{excerpt}", now + timedelta(days=days))
        return Applied(task_id, f"task:later:{days}")
    if label in ("not_interested", "unsubscribe"):
        await _stop_enrollments(db, company, "unsubscribed" if label == "unsubscribe" else "refused", now)
        if label == "unsubscribe":
            if touch.address:
                await _add_dnc(db, company.workspace_id, "email", touch.address.lower(), "unsubscribed", None,
                               "Входящие: просили не писать")
            await _add_dnc(db, company.workspace_id, "company", str(company.id), "unsubscribed", None,
                           "Входящие: просили не писать")
            company.stage = "dnc"
        else:
            await _add_dnc(db, company.workspace_id, "company", str(company.id), "refused",
                           now + timedelta(days=outreach.REFUSAL_DNC_DAYS), "Входящие: отказ")
            company.stage = "rejected"
        await _move_deal(db, company, STAGE_LOST, note=f"Отказ по почте: «{excerpt}»")
        await db.flush()
        await service.recalc_score(db, company, now=now)
        return Applied(None, "closed")
    await db.flush()
    return Applied()


# ---------------------------------------------------------------- ответ из портала

async def reply_blocker(db: AsyncSession, touch: LgTouch, company: LgCompany) -> str | None:
    if touch.label == "unsubscribe" or company.stage == "dnc":
        return "Компания просила не писать — ответ отправить нельзя"
    if not touch.mailbox_id or await db.get(LgMailbox, touch.mailbox_id) is None:
        return "Ящик, на который пришёл ответ, удалён"
    if not touch.address:
        return "Не знаем адрес отправителя"
    blocked = await db.scalar(select(LgDnc.id).where(LgDnc.workspace_id == company.workspace_id, LgDnc.kind == "email",
                                                     LgDnc.value_norm == touch.address.lower(),
                                                     or_(LgDnc.until.is_(None), LgDnc.until > service.utcnow())))
    return "Адрес в стоп-листе" if blocked else None


class ReplyError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


async def send_reply(db: AsyncSession, transport, touch: LgTouch, company: LgCompany, body: str, *,
                     user_id: int | None = None, now: datetime | None = None) -> LgTouch:
    from email.utils import make_msgid
    from app.domains.leadgen.sequences import OutgoingMail, RecipientRefused
    now = now or service.utcnow()
    blocker = await reply_blocker(db, touch, company)
    if blocker:
        raise ReplyError(blocker)
    mailbox = await db.get(LgMailbox, touch.mailbox_id)
    subject = touch.subject or ""
    subject = subject if re.match(r"^\s*re\s*:", subject, re.I) else f"Re: {subject}".strip()
    message_id = make_msgid(domain=mailbox.email.split("@")[-1])
    mail = OutgoingMail(mailbox.email, mailbox.sender_name, touch.address, subject[:300], body, message_id,
                        touch.external_id)
    try:
        await transport.send(mailbox, decrypt_secret(mailbox.password_encrypted), mail)
    except RecipientRefused as exc:
        raise ReplyError(f"Почтовый сервер не принял адрес: {exc}", 422) from exc
    except Exception as exc:  # noqa: BLE001
        mailbox.last_error = f"{type(exc).__name__}: {exc}"[:500]
        await db.flush()
        raise ReplyError(f"Не удалось отправить: {mailbox.last_error}", 502) from exc
    contact = await db.get(LgContact, touch.contact_id) if touch.contact_id else None
    out = LgTouch(company_id=company.id, contact_id=contact.id if contact else None, channel="email", direction="out",
                  sequence_id=touch.sequence_id, campaign_kind="manual_reply", campaign_id=touch.campaign_id,
                  mailbox_id=mailbox.id, status="sent", subject=subject[:300], body=body, external_id=message_id,
                  address=touch.address, user_id=user_id, happened_at=now)
    db.add(out)
    touch.handled_at = touch.handled_at or now
    await db.flush()
    return out
