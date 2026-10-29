"""Движок цепочек касаний: запись компаний, шаги по расписанию (письмо / задача в CRM), разбор входящих.

Сеть — только через MailTransport (SMTP/IMAP в отдельном потоке). В тестах — подставной транспорт.
Каждое касание пишется в lg_touches; ключ цепочки в touch.sequence_id = "enr<id>" (его понимает can_contact).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.domains.leadgen import outreach, service
from app.domains.leadgen.core import sequence as core
from app.domains.leadgen.models import (  # noqa: I001
    LgCampaign,
    LgAd, LgCompany, LgContact, LgDnc, LgEnrollment, LgMailbox, LgSequence, LgTouch,
)
from app.models.crm import CrmActivity, CrmDeal, CrmStage, CrmTask

logger = logging.getLogger(__name__)
BATCH = 100
NO_CAPACITY_RETRY = timedelta(hours=1)
CAMPAIGN_IDLE_RETRY = timedelta(minutes=30)   # кампания на паузе/в черновике — проверяем снова через полчаса
STAGE_WRITTEN, STAGE_REPLIED, STAGE_LOST = "Написали", "Ответил", "Отказ"


def seq_key(enrollment: LgEnrollment) -> str:
    return f"enr{enrollment.id}"


# ---------------------------------------------------------------- почтовый транспорт

@dataclass
class OutgoingMail:
    sender: str
    sender_name: str | None
    to: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None = None
    cc: str | None = None


@dataclass
class IncomingMail:
    uid: int
    sender: str
    subject: str
    body: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)


class RecipientRefused(Exception):
    pass


class MailTransport(Protocol):
    async def send(self, mailbox: LgMailbox, password: str, mail: OutgoingMail) -> None: ...
    async def fetch(self, mailbox: LgMailbox, password: str, after_uid: int | None) -> list[IncomingMail]: ...
    async def check(self, mailbox: LgMailbox, password: str) -> None: ...


def build_message(mail: OutgoingMail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mail.sender_name or "", mail.sender))
    msg["To"] = mail.to
    if mail.cc:
        msg["Cc"] = mail.cc   # send_message отправит и на копию
    msg["Subject"] = mail.subject
    msg["Message-ID"] = mail.message_id
    if mail.in_reply_to:
        msg["In-Reply-To"] = mail.in_reply_to
        msg["References"] = mail.in_reply_to
    # Простая строка отказа — и в теле письма, и в заголовке для почтовиков.
    msg["List-Unsubscribe"] = f"<mailto:{mail.sender}?subject=unsubscribe>"
    if core.has_markup(mail.body):   # жирный/курсив/ссылки — HTML-версия + простой текст для старых почтовиков
        msg.set_content(core.to_plain(mail.body))
        msg.add_alternative(core.to_html(mail.body), subtype="html")
    else:
        msg.set_content(mail.body)
    return msg


def _decoded(part) -> str:
    return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")


def _text_of(msg: email.message.Message) -> str:
    """Текст письма: text/plain, а если его нет (мобильная Яндекс Почта шлёт только HTML) — HTML в текст."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                text = _decoded(part)
                return core.html_to_text(text) if core.looks_html(text) else text
        for part in msg.walk():
            if part.get_content_type() == "text/html" and "attachment" not in str(part.get("Content-Disposition", "")):
                return core.html_to_text(_decoded(part))
        for part in msg.walk():
            if part.get_content_type() == "message/delivery-status":
                return str(part.get_payload())
        return ""
    text = _decoded(msg)
    return core.html_to_text(text) if msg.get_content_type() == "text/html" or core.looks_html(text) else text


_SPAM_NAMES = {"spam", "junk", "junk e-mail", "junk email", "[gmail]/spam", "&bceepwqwbdw-", "bulk mail"}


def _norm_mid(value: str) -> str:
    return (value or "").strip().strip("<>").strip().lower()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _spam_folders(box) -> list[str]:
    """Папки «Спам» по атрибуту \\Junk (RFC 6154), иначе по известным именам (у Mail.ru — «Спам» в UTF-7)."""
    typ, data = box.list()
    found, named = [], []
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if "\\junk" in m.group("flags").lower():
            found.append(name)
        elif name.lower() in _SPAM_NAMES or name.lower().endswith("/spam"):
            named.append(name)
    return found or named


def _imap_fetch_new(box, after_uid) -> list[IncomingMail]:
    box.select("INBOX", readonly=True)
    criteria = f"UID {after_uid + 1}:*" if after_uid else "SINCE " + (datetime.utcnow() - timedelta(days=3)).strftime("%d-%b-%Y")
    typ, data = box.uid("search", None, criteria)
    out = []
    for raw_uid in (data[0] or b"").split()[-200:]:
        uid = int(raw_uid)
        if after_uid and uid <= after_uid:
            continue
        typ, msgdata = box.uid("fetch", raw_uid, "(BODY.PEEK[])")
        raw = next((part[1] for part in msgdata if isinstance(part, tuple)), b"")
        msg = email.message_from_bytes(raw)
        refs = (msg.get("References") or "").split()
        out.append(IncomingMail(uid=uid, sender=parseaddr(msg.get("From", ""))[1].lower(),
                                subject=str(email.header.make_header(email.header.decode_header(msg.get("Subject", "")))),
                                body=_text_of(msg)[:20000], message_id=msg.get("Message-ID"),
                                in_reply_to=msg.get("In-Reply-To"), references=refs,
                                headers={k: str(v) for k, v in msg.items() if k.lower() in (
                                    "auto-submitted", "x-autoreply", "content-type", "precedence")}))
    return out


def _imap_locate(box, message_ids, flag=frozenset(), flag_rescued=True) -> dict:
    """Поиск SEARCH HEADER Message-ID у Яндекса и Mail.ru ненадёжен, поэтому берём заголовки всех писем за 8 дней
    одной командой FETCH и сверяем Message-ID сами. Найденное — прочитано (часть — «Важное»), из «Спама» —
    во «Входящие» со звёздочкой. Возвращает {message_id: {"folder", "auth"}}."""
    wanted = {_norm_mid(m): m for m in message_ids if m}
    out: dict = {}
    since = (datetime.utcnow() - timedelta(days=8)).strftime("%d-%b-%Y")
    for folder in ["INBOX"] + _spam_folders(box):
        if len(out) == len(wanted):
            break
        typ, _ = box.select(_quote(folder))
        if typ != "OK":
            continue
        typ, data = box.uid("search", None, "SINCE", since)
        uids = (data[0] or b"").split()[-800:] if typ == "OK" and data else []
        if not uids:
            continue
        typ, rows = box.uid("fetch", b",".join(uids), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID AUTHENTICATION-RESULTS)])")
        where = "inbox" if folder == "INBOX" else "spam"
        moved = False
        for part in rows or []:
            if not isinstance(part, tuple):
                continue
            m_uid = re.search(r"UID (\d+)", part[0].decode(errors="replace"))
            headers = email.message_from_bytes(part[1])
            key = _norm_mid(headers.get("Message-ID", ""))
            if not m_uid or key not in wanted or wanted[key] in out:
                continue
            uid, mid = m_uid.group(1), wanted[key]
            raw = " ".join(str(v) for v in headers.get_all("Authentication-Results") or [])
            auth = {k.lower(): v.lower() for k, v in re.findall(r"\b(spf|dkim|dmarc)=(\w+)", raw, re.I)}
            star = mid in flag or (where == "spam" and flag_rescued)
            box.uid("store", uid, "+FLAGS", "(\\Seen \\Flagged)" if star else "(\\Seen)")
            if where == "spam" and box.uid("copy", uid, "INBOX")[0] == "OK":
                box.uid("store", uid, "+FLAGS", "(\\Deleted)")
                moved = True
            out[mid] = {"folder": where, "auth": auth}
        if moved:
            box.expunge()
    return out


class SmtpImapTransport:
    """Реальный транспорт: SMTP по SSL (465) или STARTTLS (587), IMAP по SSL. Блокирующие вызовы — в потоке."""
    timeout = 30

    async def send(self, mailbox, password, mail):
        def go():
            ctx = ssl.create_default_context()
            if mailbox.smtp_port == 465:
                server = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=self.timeout, context=ctx)
            else:
                server = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=self.timeout)
                server.starttls(context=ctx)
            try:
                server.login(mailbox.login, password)
                refused = server.send_message(build_message(mail))
                if refused:
                    raise RecipientRefused(str(refused))
            except smtplib.SMTPRecipientsRefused as exc:
                raise RecipientRefused(str(exc.recipients)) from exc
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.to_thread(go)

    # Один вход в ящик на всё: новые письма (ответы лидов) + поиск прогревочных писем. Почтовики рвут
    # соединения, если заходить слишком часто, поэтому разбор ответов и прогрев делят одно подключение.
    async def _session(self, mailbox, password, work):
        def go():
            box = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=self.timeout)
            try:
                box.login(mailbox.login, password)
                return work(box)
            finally:
                try:
                    box.logout()
                except Exception:  # noqa: BLE001
                    pass
        try:
            return await asyncio.to_thread(go)
        except (OSError, imaplib.IMAP4.abort):
            await asyncio.sleep(5)   # сервер оборвал соединение — одна повторная попытка
            return await asyncio.to_thread(go)

    async def fetch(self, mailbox, password, after_uid):
        return await self._session(mailbox, password, lambda box: _imap_fetch_new(box, after_uid))

    async def warm_locate(self, mailbox, password, message_ids, flag=frozenset(), flag_rescued=True):
        """Прогрев: найти письма по Message-ID во «Входящих» и в «Спаме», из «Спама» — перенести во «Входящие»."""
        return await self._session(mailbox, password,
                                   lambda box: _imap_locate(box, message_ids, flag, flag_rescued))

    async def fetch_and_locate(self, mailbox, password, after_uid, message_ids, flag=frozenset()):
        def work(box):
            mails = _imap_fetch_new(box, after_uid)
            return mails, (_imap_locate(box, message_ids, flag, True) if message_ids else {})
        return await self._session(mailbox, password, work)

    async def check(self, mailbox, password):
        def reachable(host, port, what):
            import socket
            try:
                socket.create_connection((host, port), timeout=8).close()
            except OSError:
                raise ConnectionError(
                    f"{what} {host}:{port} недоступен с сервера портала. Если это SMTP — хостинг закрыл исходящие "
                    f"почтовые порты: попросите поддержку Timeweb открыть 465 и 587") from None

        def go():
            reachable(mailbox.smtp_host, mailbox.smtp_port, "SMTP")
            reachable(mailbox.imap_host, mailbox.imap_port, "IMAP")
            ctx = ssl.create_default_context()
            server = (smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=self.timeout, context=ctx)
                      if mailbox.smtp_port == 465 else smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=self.timeout))
            if mailbox.smtp_port != 465:
                server.starttls(context=ctx)
            try:
                server.login(mailbox.login, password)
            except smtplib.SMTPAuthenticationError:
                raise PermissionError("Почта не приняла логин или пароль. Для Gmail и Яндекса нужен пароль приложения, "
                                      "а логин — полный адрес почты") from None
            server.quit()
            box = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=self.timeout)
            try:
                box.login(mailbox.login, password)
            except imaplib.IMAP4.error:
                raise PermissionError("IMAP не принял логин или пароль (проверьте, что IMAP включён в настройках почты)") from None
            box.logout()
        await asyncio.to_thread(go)


# ---------------------------------------------------------------- запись в цепочку

@dataclass
class EnrollResult:
    company_id: int
    ok: bool
    reason: str | None = None
    enrollment_id: int | None = None


def _uses(steps: list[core.Step], channel: str) -> bool:
    return any(s.channel == channel for s in steps)


async def _email_contact(db: AsyncSession, company_id: int) -> LgContact | None:
    return await db.scalar(select(LgContact).where(
        LgContact.company_id == company_id, LgContact.kind == "email", LgContact.is_personal.is_(False),
        LgContact.bounced.is_(False), LgContact.verify_status != "invalid")
        .order_by(LgContact.is_primary.desc(), LgContact.id).limit(1))


async def enroll(db: AsyncSession, sequence: LgSequence, company: LgCompany, *,
                 now: datetime | None = None, campaign=None, contact: LgContact | None = None,
                 vars: dict | None = None) -> EnrollResult:
    """Компания -> цепочка (или кампания). Сделку в CRM не создаём: её заводят вручную из «Входящих»,
    чтобы CRM не засорялась сотнями холодных компаний. contact — конкретный адрес из файла кампании."""
    now = now or service.utcnow()
    if not sequence.is_active:
        return EnrollResult(company.id, False, "sequence_inactive")
    steps = core.parse_steps(sequence.steps, strict=False)
    window = core.parse_window(sequence.window)
    active = await db.scalar(select(LgEnrollment.id).where(LgEnrollment.company_id == company.id,
                                                           LgEnrollment.status.in_(("active", "paused"))))
    if active:
        return EnrollResult(company.id, False, "already_in_sequence")
    contact = contact or await _email_contact(db, company.id)
    if _uses(steps, "email") and contact is None:
        return EnrollResult(company.id, False, "no_email")
    client = campaign.client_project_id if campaign is not None else None
    decision = await service.check_can_contact(db, company.id, contact.id if contact else None,
                                               "email" if contact else "call", now=now, client_project_id=client)
    if not decision.ok and decision.reason != "crm:open_deal":
        return EnrollResult(company.id, False, decision.reason)
    start = now
    if campaign is not None and campaign.start_at and service._aware(campaign.start_at) > now:
        start = service._aware(campaign.start_at)
    row = LgEnrollment(workspace_id=company.workspace_id, sequence_id=sequence.id, company_id=company.id,
                       contact_id=contact.id if contact else None, status="active", current_step=0,
                       next_at=core.step_due(start, steps[0], window), started_at=now,
                       campaign_id=campaign.id if campaign is not None else None, vars=vars or None)
    db.add(row)
    if not client:   # этапы базы — про продажи агентства; рассылка клиента их не трогает
        company.stage = "in_outreach"
    await db.flush()
    return EnrollResult(company.id, True, None, row.id)


# ---------------------------------------------------------------- шаги по расписанию

async def _context(db: AsyncSession, company: LgCompany, mailbox: LgMailbox | None) -> dict:
    ad = await db.scalar(select(LgAd).where(LgAd.company_id == company.id).order_by(LgAd.last_seen_at.desc()).limit(1))
    return {"company": company.display_name or company.legal_name or company.domain, "domain": company.domain,
            "city": company.city, "niche": company.niche, "ad_title": ad.title if ad else None,
            "ad_text": ad.text if ad else None, "director_name": company.director_name,
            "director_first_name": core.first_name(company.director_name),
            "sender_name": mailbox.sender_name if mailbox else None}


async def _sent_today(db: AsyncSession, mailbox_id: int, now: datetime, tz: str) -> int:
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.scalar(select(func.count(LgTouch.id)).where(
        LgTouch.mailbox_id == mailbox_id, LgTouch.channel == "email", LgTouch.direction == "out",
        LgTouch.status.in_(("sent", "delivered", "replied")), LgTouch.happened_at >= start)) or 0


async def _ready_at(db: AsyncSession, mailbox_id: int, gap_minutes: int) -> datetime | None:
    """Когда ящик снова можно использовать: последнее письмо + пауза с разбросом."""
    last = (await db.execute(select(LgTouch.id, LgTouch.happened_at).where(
        LgTouch.mailbox_id == mailbox_id, LgTouch.channel == "email", LgTouch.direction == "out",
        LgTouch.status.in_(("sent", "delivered", "replied"))).order_by(LgTouch.happened_at.desc()).limit(1))).first()
    if not gap_minutes or not last or not last[1]:
        return None
    return service._aware(last[1]) + core.gap_for(gap_minutes, last[0])


async def _campaign_share(db: AsyncSession, mb: LgMailbox, campaign, now: datetime, tz: str, limit: int) -> int:
    """Сколько ещё может отправить с ящика эта кампания сегодня: лимит ящика делится поровну между активными
    кампаниями, у которых есть кому писать. Пока другим нечего слать, кампания берёт весь остаток ящика."""
    from math import ceil
    campaigns = (await db.scalars(select(LgCampaign).where(LgCampaign.workspace_id == mb.workspace_id,
                                                           LgCampaign.status == "active"))).all()
    sharing = []
    for c in campaigns:
        if mb.id not in (c.mailbox_ids or []):
            continue
        waiting = await db.scalar(select(LgEnrollment.id).where(
            LgEnrollment.campaign_id == c.id, LgEnrollment.status == "active", LgEnrollment.mailbox_id.is_(None)
            | (LgEnrollment.mailbox_id == mb.id), LgEnrollment.next_at <= now).limit(1))
        if waiting or c.id == campaign.id:
            sharing.append(c)
    share = ceil(limit / max(1, len(sharing)))
    local = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    mine = await db.scalar(select(func.count(LgTouch.id)).where(
        LgTouch.mailbox_id == mb.id, LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == campaign.sequence_id,
        LgTouch.channel == "email", LgTouch.direction == "out", LgTouch.status.in_(("sent", "delivered", "replied", "bounced")),
        LgTouch.happened_at >= local)) or 0
    return max(0, share - mine)


async def _choose_mailbox(db: AsyncSession, enrollment: LgEnrollment, now: datetime, tz: str,
                          gap_minutes: int = 10, campaign=None) -> tuple[LgMailbox | None, datetime | None]:
    """Ящик закрепляется за компанией: вся ветка писем идёт от одного отправителя.
    Возвращает (ящик, None) или (None, когда попробовать снова, если мешает только пауза между письмами)."""
    from app.domains.leadgen import warmup

    async def capacity(mb: LgMailbox) -> int:
        local_today = now.astimezone(ZoneInfo(tz)).date()
        limit = warmup.cold_limit(mb, local_today)   # первые 7 дней прогрева — 0
        left = warmup.cold_capacity(mb, max(0, limit - await _sent_today(db, mb.id, now, tz)))
        if campaign is not None and left > 0:
            left = min(left, await _campaign_share(db, mb, campaign, now, tz, warmup.cold_capacity(mb, limit)))
        return left
    if enrollment.mailbox_id:   # продолжение ветки — только с того же ящика
        boxes = [mb for mb in [await db.get(LgMailbox, enrollment.mailbox_id)] if mb and mb.is_active]
    else:
        query = select(LgMailbox).where(LgMailbox.workspace_id == enrollment.workspace_id, LgMailbox.is_active.is_(True),
                                        LgMailbox.role != "helper")
        if campaign is not None:
            query = query.where(LgMailbox.id.in_(campaign.mailbox_ids or [-1]))
        boxes = (await db.execute(query)).scalars().all()
    candidates, soonest = [], None
    for mb in boxes:
        left = await capacity(mb)
        if left <= 0:
            continue
        ready = await _ready_at(db, mb.id, gap_minutes)
        if ready and ready > now:
            soonest = ready if soonest is None or ready < soonest else soonest
            continue
        candidates.append((mb.id, left))
    picked = core.pick_mailbox(candidates)
    return next((mb for mb in boxes if mb.id == picked), None), soonest


async def _new_today(db: AsyncSession, sequence_id: int, now: datetime, tz: str) -> int:
    """Сколько компаний получили ПЕРВОЕ письмо этой цепочки сегодня (лимит «новых в день»)."""
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    first = (select(LgTouch.sequence_id, func.min(LgTouch.happened_at).label("first_at"))
             .where(LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == sequence_id,
                    LgTouch.direction == "out", LgTouch.channel == "email").group_by(LgTouch.sequence_id).subquery())
    return await db.scalar(select(func.count()).select_from(first).where(first.c.first_at >= start)) or 0


async def _move_deal(db: AsyncSession, company: LgCompany, stage_name: str, note: str | None = None,
                     only_from_first: bool = False) -> None:
    if not company.crm_deal_id:
        return
    deal = await db.get(CrmDeal, company.crm_deal_id)
    if deal is None:
        return
    target = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id, CrmStage.name == stage_name,
                                                    CrmStage.archived_at.is_(None)))
    current = await db.get(CrmStage, deal.stage_id)
    if target is None or current is None or current.analytics_type in ("WON", "LOST"):
        return
    if only_from_first and current.position != 0:
        return
    if target.id != deal.stage_id and target.position > current.position:
        deal.stage_id, deal.stage_entered_at = target.id, service.utcnow()
        if target.analytics_type == "LOST":
            deal.lost_at = deal.closed_at = service.utcnow()
    if note:
        db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                           actor_name="Аутрич", event_type="COMMENT_ADDED", payload={"text": note}))
    deal.last_activity_at = service.utcnow()


def _finish(enrollment: LgEnrollment, status: str, reason: str | None, now: datetime) -> None:
    enrollment.status, enrollment.stop_reason, enrollment.finished_at, enrollment.next_at = status, reason, now, None


async def process_due(db: AsyncSession, transport: MailTransport, *, now: datetime | None = None,
                      limit: int = BATCH) -> dict:
    """Выполняет созревшие шаги. Коммитить — вызывающему (после каждого вызова)."""
    now = now or service.utcnow()
    stats = {"emails": 0, "tasks": 0, "postponed": 0, "stopped": 0, "finished": 0, "errors": 0}
    due = (await db.execute(select(LgEnrollment).where(LgEnrollment.status == "active", LgEnrollment.next_at <= now)
                            .order_by(LgEnrollment.next_at).limit(limit))).scalars().all()
    for enr in due:
        sequence = await db.get(LgSequence, enr.sequence_id)
        company = await db.get(LgCompany, enr.company_id)
        if sequence is None or company is None or not sequence.is_active:
            _finish(enr, "stopped", "sequence_inactive", now); stats["stopped"] += 1
            continue
        steps, window = core.parse_steps(sequence.steps, strict=False), core.parse_window(sequence.window)
        if enr.campaign_id:
            campaign = await db.get(LgCampaign, enr.campaign_id)
            if campaign is None or campaign.status in ("done",):
                _finish(enr, "stopped", "campaign_done", now); stats["stopped"] += 1
                continue
            if campaign.status != "active":       # черновик или пауза — ждём, ничего не теряем
                enr.next_at = now + CAMPAIGN_IDLE_RETRY; stats["postponed"] += 1
                continue
            if campaign.start_at and service._aware(campaign.start_at) > now:
                enr.next_at = core.in_window(service._aware(campaign.start_at), window); stats["postponed"] += 1
                continue
        if enr.current_step >= len(steps):
            _finish(enr, "finished", None, now); stats["finished"] += 1
            continue
        step = steps[enr.current_step]
        if step.channel == "email":
            outcome = await _send_email_step(db, transport, enr, company, step, window, now)
        else:
            outcome = await _task_step(db, enr, company, step, now)
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome in ("emails", "tasks", "skipped"):
            enr.current_step += 1
            if enr.current_step >= len(steps):
                _finish(enr, "finished", None, now); stats["finished"] += 1
            else:
                enr.next_at = core.step_due(now, steps[enr.current_step], window)
    await db.flush()
    return stats


async def _send_email_step(db, transport, enr, company, step, window, now) -> str:
    contact = await db.get(LgContact, enr.contact_id) if enr.contact_id else None
    if contact is None:
        _finish(enr, "stopped", "no_email", now)
        return "stopped"
    decision = await service.check_can_contact(db, company.id, contact.id, "email", sequence_id=seq_key(enr), now=now,
                                               client_project_id=await _client_of(db, enr))
    if not decision.ok and decision.reason != "crm:open_deal":
        _finish(enr, "stopped", decision.reason, now)
        return "stopped"
    if enr.current_step == 0 and window.daily_new and await _new_today(db, enr.sequence_id, now, window.tz) >= window.daily_new:
        tomorrow = now.astimezone(ZoneInfo(window.tz)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        enr.next_at = core.in_window(tomorrow, window)  # лимит новых на сегодня — первое письмо завтра
        return "postponed"
    campaign = await db.get(LgCampaign, enr.campaign_id) if enr.campaign_id else None
    mailbox, retry_at = await _choose_mailbox(db, enr, now, window.tz, window.gap_minutes, campaign=campaign)
    if mailbox is None:
        # Пауза между письмами — пробуем, когда ящик освободится; дневной лимит — через час/в следующее окно.
        enr.next_at = core.in_window(retry_at if retry_at else now + NO_CAPACITY_RETRY, window)
        return "postponed"
    ctx = {**await _context(db, company, mailbox), **{k.lower(): v for k, v in (enr.vars or {}).items() if v not in (None, "")}}
    seed = f"{enr.id}:{enr.current_step}"
    first_in_thread = step.new_thread or not enr.thread_message_id
    subject = core.render(step.subject or "", ctx, seed) if (first_in_thread or step.subject) else ""
    if not first_in_thread:
        subject = subject or f"Re: {enr.thread_subject}"
    body = core.render(step.body, ctx, seed)
    message_id = make_msgid(domain=mailbox.email.split("@")[-1])
    mail = OutgoingMail(mailbox.email, mailbox.sender_name, contact.value_norm, subject, body, message_id,
                        None if first_in_thread else enr.thread_message_id)
    try:
        await transport.send(mailbox, decrypt_secret(mailbox.password_encrypted), mail)
    except RecipientRefused as exc:
        contact.bounced, contact.verify_status = True, "invalid"
        db.add(LgTouch(company_id=company.id, contact_id=contact.id, channel="email", direction="out",
                       sequence_id=seq_key(enr), campaign_kind="sequence", campaign_id=enr.sequence_id,
                       mailbox_id=mailbox.id, status="bounced", subject=subject, body=body, happened_at=now))
        await _dnc(db, company.workspace_id, "email", contact.value_norm, "bounced")
        _finish(enr, "bounced", "recipient_refused", now)
        logger.info("recipient refused %s: %s", contact.value_norm, exc)
        return "stopped"
    except Exception as exc:  # noqa: BLE001 — ящик недоступен: ошибку видно в настройках, шаг повторится
        mailbox.last_error = f"{type(exc).__name__}: {exc}"[:500]
        enr.next_at = core.in_window(now + NO_CAPACITY_RETRY, window)
        return "errors"
    mailbox.last_error = None
    if first_in_thread:
        enr.thread_message_id, enr.thread_subject = message_id, subject
    enr.mailbox_id = mailbox.id
    db.add(LgTouch(company_id=company.id, contact_id=contact.id, channel="email", direction="out",
                   sequence_id=seq_key(enr), campaign_kind="sequence", campaign_id=enr.sequence_id,
                   mailbox_id=mailbox.id, status="sent", subject=subject, body=body, external_id=message_id,
                   address=contact.value_norm, happened_at=now))
    await _move_deal(db, company, STAGE_WRITTEN, note=f"Отправлено письмо «{subject}» на {contact.value_norm}",
                     only_from_first=True)
    return "emails"


async def _task_step(db, enr, company, step, now) -> str:
    deal = await db.get(CrmDeal, company.crm_deal_id) if company.crm_deal_id else None
    if deal is None:
        return "skipped"   # задача «позвонить/написать» — только для компаний, которых завели в CRM; цепочка идёт дальше
    type_code, label = core.TASK_CHANNELS[step.channel]
    ctx = await _context(db, company, None)
    db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                   contact_id=deal.contact_id, type_code=type_code, title=f"{label}: {ctx['company']}",
                   description=core.render(step.body, ctx) or None, due_at=now, priority="NORMAL", status="OPEN"))
    db.add(LgTouch(company_id=company.id, channel=step.channel, direction="out", sequence_id=seq_key(enr),
                   campaign_kind="sequence", campaign_id=enr.sequence_id, status="planned",
                   body=core.render(step.body, ctx), happened_at=now))
    return "tasks"


async def _dnc(db: AsyncSession, workspace_id: int, kind: str, value: str, reason: str,
               client_project_id: int | None = None) -> None:
    """Стоп-лист. Отписка от рассылки клиента — в стоп-лист этого клиента; bounce — общий."""
    scope = LgDnc.client_project_id == client_project_id if client_project_id else LgDnc.client_project_id.is_(None)
    exists = await db.scalar(select(LgDnc.id).where(LgDnc.workspace_id == workspace_id, LgDnc.kind == kind,
                                                    LgDnc.value_norm == value, scope))
    if not exists:
        db.add(LgDnc(workspace_id=workspace_id, kind=kind, value_norm=value, reason=reason,
                     client_project_id=client_project_id, note="Автоматически: цепочка касаний"))


async def _client_of(db: AsyncSession, enr: LgEnrollment | None) -> int | None:
    """Проект клиента, от имени которого идёт кампания (None — пишет агентство)."""
    if enr is None or not enr.campaign_id:
        return None
    from app.domains.leadgen.models import LgCampaign
    return await db.scalar(select(LgCampaign.client_project_id).where(LgCampaign.id == enr.campaign_id))


# ---------------------------------------------------------------- входящие

async def _match(db: AsyncSession, workspace_id: int, mail: IncomingMail) -> tuple[LgEnrollment | None, LgContact | None]:
    ids = [i.strip() for i in [mail.in_reply_to, *mail.references] if i]
    if ids:
        touch = await db.scalar(select(LgTouch).where(LgTouch.external_id.in_(ids)).order_by(LgTouch.id.desc()).limit(1))
        if touch is not None and touch.sequence_id and touch.sequence_id.startswith("enr"):
            enr = await db.get(LgEnrollment, int(touch.sequence_id[3:]))
            if enr is not None and enr.workspace_id == workspace_id:
                return enr, await db.get(LgContact, touch.contact_id) if touch.contact_id else None
    sender = mail.sender.lower()
    contact = await db.scalar(select(LgContact).join(LgCompany, LgCompany.id == LgContact.company_id).where(
        LgCompany.workspace_id == workspace_id, LgContact.kind == "email", LgContact.value_norm == sender).limit(1))
    if contact is None:
        return None, None
    enr = await db.scalar(select(LgEnrollment).where(LgEnrollment.company_id == contact.company_id)
                          .order_by(LgEnrollment.id.desc()).limit(1))
    return enr, contact


async def handle_inbound(db: AsyncSession, mailbox: LgMailbox, mail: IncomingMail, *,
                         now: datetime | None = None) -> str:
    """Один входящий: bounce / auto_reply / unsubscribe / reply / unmatched."""
    now = now or service.utcnow()
    kind = core.classify_inbound(mail.sender, mail.subject, mail.body, mail.headers)
    if kind == "bounce":
        address = core.bounced_address(mail.body)
        contacts = (await db.execute(select(LgContact).join(LgCompany, LgCompany.id == LgContact.company_id).where(
            LgCompany.workspace_id == mailbox.workspace_id, LgContact.kind == "email",
            LgContact.value_norm == address))).scalars().all() if address else []
        for c in contacts:
            c.bounced, c.verify_status = True, "invalid"
            for enr in (await db.execute(select(LgEnrollment).where(LgEnrollment.contact_id == c.id,
                                                                    LgEnrollment.status.in_(("active", "paused"))))).scalars():
                _finish(enr, "bounced", "bounce", now)
        if address:
            await _dnc(db, mailbox.workspace_id, "email", address, "bounced")
            original = await db.scalar(select(LgTouch).where(
                LgTouch.direction == "out", LgTouch.channel == "email", LgTouch.address == address,
                LgTouch.status == "sent").order_by(LgTouch.id.desc()).limit(1))
            if original is not None:
                original.status = "bounced"
        await db.flush()
        return "bounce" if contacts else "unmatched"

    enr, contact = await _match(db, mailbox.workspace_id, mail)
    if enr is None:
        return "unmatched"
    company = await db.get(LgCompany, enr.company_id)
    fresh = core.reply_text(mail.body)[:4000]
    from app.domains.leadgen import inbox
    label, source = inbox.initial_label(kind, fresh)
    status = {"auto_reply": "auto", "unsubscribe": "unsubscribed"}.get(kind, "replied")
    touch = LgTouch(company_id=company.id, contact_id=contact.id if contact else None, channel="email",
                    direction="in", sequence_id=seq_key(enr), campaign_kind="sequence", campaign_id=enr.sequence_id,
                    mailbox_id=mailbox.id, status=status, subject=mail.subject[:300], body=fresh,
                    external_id=(mail.message_id or "")[:300] or None, address=(mail.sender or "")[:254] or None,
                    label=label, label_source=source, handled_at=now if label in inbox.SELF_HANDLED else None,
                    happened_at=now)
    db.add(touch)
    await db.flush()
    if kind != "auto_reply":
        await inbox.merge_earlier(db, touch, now)
    if kind == "auto_reply":
        await db.flush()
        return kind
    if enr.status in ("active", "paused"):
        _finish(enr, "replied" if kind == "reply" else "stopped", "unsubscribed" if kind == "unsubscribe" else "reply", now)
    # Ответил кто угодно из компании — останавливаем и остальные её цепочки и кампании.
    for other in (await db.scalars(select(LgEnrollment).where(LgEnrollment.company_id == enr.company_id,
                                                              LgEnrollment.id != enr.id,
                                                              LgEnrollment.status.in_(("active", "paused"))))).all():
        _finish(other, "stopped", "company_replied", now)
    client = await _client_of(db, enr)
    from app.domains.leadgen import client_outreach
    if kind == "unsubscribe" and client:
        if contact is not None:
            await _dnc(db, company.workspace_id, "email", contact.value_norm, "unsubscribed", client)
        await _dnc(db, company.workspace_id, "company", str(company.id), "unsubscribed", client)
        await client_outreach.close_deal(db, enr, f"Отписался от рассылки: «{fresh[:500]}»", now)
        await db.flush()
    elif kind == "unsubscribe":
        if contact is not None:
            await _dnc(db, company.workspace_id, "email", contact.value_norm, "unsubscribed")
        await _dnc(db, company.workspace_id, "company", str(company.id), "unsubscribed")
        company.stage = "dnc"
        await _move_deal(db, company, STAGE_LOST, note=f"Отказ по почте: «{fresh[:500]}»")
        await db.flush()
        await service.recalc_score(db, company, now=now)
    elif client:
        await db.flush()
        from app.domains.leadgen import hot
        await hot.mark(db, touch, company, score=None, signals=hot.rule_signals(fresh), now=now)
        await client_outreach.after_reply(db, touch, company, now=now)
    else:
        company.stage = "replied"
        await _move_deal(db, company, STAGE_REPLIED, note=f"Ответ на письмо от {mail.sender}:\n{fresh}")
        await db.flush()
        # Явные признаки (телефон, «созвонимся») ловим сразу правилами, не дожидаясь ИИ.
        from app.domains.leadgen import hot
        await hot.mark(db, touch, company, score=None, signals=hot.rule_signals(fresh), now=now)
    return kind


async def poll_mailbox(db: AsyncSession, transport: MailTransport, mailbox: LgMailbox, *,
                       now: datetime | None = None, warm: bool = False) -> dict:
    """Один вход в ящик: новые письма (ответы лидов) и, если warm, поиск прогревочных писем."""
    from app.domains.leadgen import warmup
    now = now or service.utcnow()
    counts: dict[str, int] = {}
    rows = await warmup.pending_for(db, mailbox, now) if warm and mailbox.warmup_enabled else []
    ids = [r.message_id for r in rows if r.message_id]
    password = decrypt_secret(mailbox.password_encrypted)
    try:
        if ids and hasattr(transport, "fetch_and_locate"):
            mails, found = await transport.fetch_and_locate(mailbox, password, mailbox.imap_last_uid, ids,
                                                            warmup.flags_for(rows))
        else:
            mails = await transport.fetch(mailbox, password, mailbox.imap_last_uid)
            found = await transport.warm_locate(mailbox, password, ids, warmup.flags_for(rows)) if ids else {}
    except Exception as exc:  # noqa: BLE001
        warmup.imap_result(mailbox, now, exc)
        mailbox.last_error, mailbox.last_checked_at = mailbox.warmup_error, now
        await db.flush()
        return {"error": 1}
    warmup.imap_result(mailbox, now)
    if rows:
        await warmup.apply_found(db, mailbox, rows, found, now)
    ours = await warmup.pool_addresses(db, mailbox.workspace_id)
    for mail in sorted(mails, key=lambda m: m.uid):
        # Письма между нашими ящиками — прогрев: не ответы лидов, во «Входящие» и аналитику не идут.
        kind = "warmup" if (mail.sender or "").lower() in ours else await handle_inbound(db, mailbox, mail, now=now)
        counts[kind] = counts.get(kind, 0) + 1
        mailbox.imap_last_uid = max(mailbox.imap_last_uid or 0, mail.uid)
    mailbox.last_checked_at = now
    await db.flush()
    return counts


# ---------------------------------------------------------------- фоновый воркер

TICK_SECONDS = 60
POLL_EVERY_TICKS = 5


async def run_worker() -> None:
    from app.domains.leadgen import routes
    await asyncio.sleep(60)
    tick = 0
    while True:
        try:
            transport = routes.mail_transport_factory()
            async with routes.session_factory() as db:
                await process_due(db, transport)
                await db.commit()
            async with routes.session_factory() as db:
                from app.domains.leadgen import hot
                if await hot.remind_due(db):
                    await db.commit()
            from app.domains.leadgen import warmup
            warm_check = tick % warmup.CHECK_EVERY_TICKS == 0   # каждые 15 минут, внутри обычного опроса ящика
            if tick % POLL_EVERY_TICKS == 0:
                async with routes.session_factory() as db:
                    boxes = (await db.execute(select(LgMailbox).where(LgMailbox.is_active.is_(True)))).scalars().all()
                    for mb in boxes:
                        try:
                            await poll_mailbox(db, transport, mb, warm=warm_check)
                            await db.commit()
                        except Exception:  # noqa: BLE001 — один ящик не должен ломать опрос остальных
                            await db.rollback()
                            logger.exception("leadgen poll failed for mailbox %s", mb.id)
                from app.domains.leadgen import inbox
                if inbox.available():
                    async with routes.session_factory() as db:
                        await inbox.classify_pending(db)
                        await db.commit()
            try:
                async with routes.session_factory() as db:
                    await warmup.run_tick(db, transport, check=warm_check)
                    await db.commit()
            except Exception:  # noqa: BLE001 — прогрев не должен останавливать цепочки
                logger.exception("leadgen warmup tick failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("leadgen sequences tick failed")
        tick += 1
        await asyncio.sleep(TICK_SECONDS)
