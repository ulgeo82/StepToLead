"""Кампании аутрича, как в Instantly: своя база лидов (CSV/Excel или «База компаний»), своя цепочка, свои ящики.

Загрузка файла в два шага: preview — какие столбцы что значат (email, компания, сайт, имя…, остальные — свои
переменные {{боль}}), import — строки -> компании базы (без дублей) -> участники кампании со своими переменными.
Перед записью каждый адрес проверяется: формат, у домена есть почта (MX), нет в стоп-листе, ему не писали недавно,
он не в другой кампании. Всё, что не прошло, — в отчёт с причиной. Сделки в CRM здесь не создаются.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import lists, service
from app.domains.leadgen.core import sequence as core
from app.domains.leadgen.core import table_import
from app.domains.leadgen.models import (
    LgCampaign, LgCompany, LgContact, LgEnrollment, LgList, LgMailbox, LgSequence, LgTouch,
)

MAX_LEADS = 5000
FIELDS = {  # что можно выбрать для столбца
    "email": "Email", "company": "Компания", "website": "Сайт", "first_name": "Имя", "city": "Город",
    "niche": "Ниша", "phone": "Телефон", "var": "Своя переменная", "skip": "Не загружать",
}
AUTO = {"domain": "website", "name": "company", "city": "city", "niche": "niche", "phone": "phone", "email": "email"}
FIRST_NAME = ("имя", "first name", "firstname", "first_name", "name first", "имя контакта", "имя лпр")
STANDARD_VARS = {"company": "company", "first_name": "director_first_name", "city": "city", "niche": "niche"}
DEFAULT_STEPS = [{"channel": "email", "delay_days": 0, "subject": "{Вопрос|Идея} для {{company}}",
                  "body": "{Добрый день|Здравствуйте}, {{director_first_name|коллеги}}!\n\n…\n\n"
                          "Если неактуально — ответьте «нет», больше не напишу."}]
DEFAULT_WINDOW = {"tz": "Europe/Moscow", "start_hour": 10, "end_hour": 18, "weekdays": [0, 1, 2, 3, 4],
                  "gap_minutes": 10, "daily_new": 30}
REASONS = {
    "bad_email": "некорректный адрес", "duplicate": "повтор в файле", "no_mx": "у домена нет почты",
    "no_company": "не удалось определить компанию", "already_in_sequence": "уже в другой кампании или цепочке",
    "recently_contacted": "писали недавно", "dnc:company": "в стоп-листе", "dnc:domain": "домен в стоп-листе",
    "dnc:email": "адрес в стоп-листе", "stage:rejected": "компания отказалась", "stage:dnc": "в стоп-листе",
    "crm:client": "уже клиент", "contact:bounced": "адрес не существует (был недоставлен)",
    "contact:invalid": "адрес недействителен", "contact:personal": "личный адрес сотрудника",
    "no_email": "нет рабочей почты", "sequence:another_active": "идёт другая цепочка",
}


class CampaignError(Exception):
    pass


# ---------------------------------------------------------------- создание

async def create(db: AsyncSession, workspace_id: int, *, name: str, template: LgSequence | None,
                 mailbox_ids: list[int], user_id: int | None = None) -> LgCampaign:
    """Цепочка кампании — копия шаблона: правки в одной кампании не задевают другие."""
    seq = LgSequence(workspace_id=workspace_id, name=f"{name} — цепочка"[:180],
                     steps=[dict(s) for s in (template.steps if template else DEFAULT_STEPS)],
                     window=dict(template.window) if template and template.window else dict(DEFAULT_WINDOW),
                     is_active=True, campaign_owned=True, created_by_id=user_id)
    db.add(seq)
    await db.flush()
    campaign = LgCampaign(workspace_id=workspace_id, name=name, status="draft", sequence_id=seq.id,
                          mailbox_ids=await valid_mailboxes(db, workspace_id, mailbox_ids), variables=[],
                          created_by_id=user_id)
    db.add(campaign)
    await db.flush()
    return campaign


async def valid_mailboxes(db: AsyncSession, workspace_id: int, ids: list[int]) -> list[int]:
    if not ids:
        return []
    rows = (await db.scalars(select(LgMailbox.id).where(LgMailbox.workspace_id == workspace_id,
                                                        LgMailbox.id.in_(ids), LgMailbox.role != "helper"))).all()
    return sorted(set(rows))


async def campaign_list(db: AsyncSession, campaign: LgCampaign) -> LgList:
    """У каждой кампании свой список в базе — её компании видно фильтром «Список»."""
    lst = await db.scalar(select(LgList).where(LgList.workspace_id == campaign.workspace_id,
                                               LgList.kind == "campaign", LgList.note == f"campaign:{campaign.id}"))
    if lst is None:
        lst = LgList(workspace_id=campaign.workspace_id, name=f"Кампания · {campaign.name}"[:180], kind="campaign",
                     note=f"campaign:{campaign.id}", created_by_id=campaign.created_by_id)
        db.add(lst)
        await db.flush()
    return lst


# ---------------------------------------------------------------- файл: столбцы

def read_cells(data: bytes, filename: str) -> list[list[str]]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        cells = table_import._cells_from_xlsx(data)
    elif name.endswith(".xls"):
        raise CampaignError("Старый формат .xls не поддерживается — сохраните файл как .xlsx или .csv")
    else:
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise CampaignError("Не удалось прочитать файл: сохраните его как CSV в UTF-8")
        cells = table_import._cells_from_text(text)
    cells = [[c.strip() for c in r] for r in cells if any(c.strip() for c in r)]
    if len(cells) < 2:
        raise CampaignError("В файле нет строк с лидами: нужна строка заголовков и хотя бы одна строка данных")
    if len(cells) - 1 > MAX_LEADS:
        raise CampaignError(f"За один раз — до {MAX_LEADS} лидов. Разбейте файл на части")
    return cells


def guess(header: str, values: list[str]) -> dict:
    h = (header or "").strip().lower().replace("ё", "е")
    if h in FIRST_NAME or h.startswith("имя "):
        return {"field": "first_name"}
    known = table_import._column_for(header)
    if known in AUTO:
        return {"field": AUTO[known]}
    if values and sum(1 for v in values if table_import._EMAIL.fullmatch(v.strip())) >= max(1, len(values) // 2):
        return {"field": "email"}
    if known:
        return {"field": "skip"}
    name = core.var_name(header)
    return {"field": "var", "var": name} if name else {"field": "skip"}


def preview(cells: list[list[str]]) -> dict:
    headers, rows = cells[0], cells[1:]
    columns = []
    for i, header in enumerate(headers):
        values = [r[i] for r in rows[:50] if i < len(r) and r[i]]
        columns.append({"index": i, "header": header or f"Столбец {i + 1}", "sample": values[:3],
                        "filled": sum(1 for r in rows if i < len(r) and r[i]), **guess(header, values)})
    if not any(c["field"] == "email" for c in columns):
        raise CampaignError("Не нашли столбец с email. Проверьте, что в файле есть адреса почты")
    return {"columns": columns, "rows": len(rows), "fields": FIELDS,
            "sample": [dict(zip(headers, r)) for r in rows[:5]]}


# ---------------------------------------------------------------- проверка адресов (MX)

def _has_mail(domain: str) -> bool | None:
    import dns.exception
    import dns.resolver
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    for rtype in ("MX", "A"):
        try:
            if resolver.resolve(domain, rtype):
                return True
        except (dns.resolver.NXDOMAIN,):
            return False
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.DNSException:
            return None   # не знаем — не отбрасываем
    return False


async def mail_domains(domains: set[str]) -> dict[str, bool | None]:
    sem = asyncio.Semaphore(10)

    async def one(d: str):
        async with sem:
            return d, await asyncio.to_thread(_has_mail, d)
    return dict(await asyncio.gather(*(one(d) for d in domains)))


# ---------------------------------------------------------------- загрузка лидов

@dataclass
class ImportReport:
    added: int = 0
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def skip(self, line: int, email: str | None, reason: str) -> None:
        self.skipped.append({"line": line, "email": email, "reason": reason, "reason_text": REASONS.get(reason, reason)})

    def out(self) -> dict:
        counts: dict[str, int] = {}
        for s in self.skipped:
            counts[s["reason_text"]] = counts.get(s["reason_text"], 0) + 1
        return {"added": self.added, "skipped": len(self.skipped), "reasons": counts,
                "skipped_rows": self.skipped[:300], "warnings": self.warnings}


def _cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None and index < len(row) else ""


async def import_leads(db: AsyncSession, campaign: LgCampaign, cells: list[list[str]], mapping: list[dict], *,
                       mx_check=None, now: datetime | None = None) -> dict:
    """mapping: [{"index", "field", "var"?}]. mx_check — подмена проверки MX в тестах."""
    now = now or service.utcnow()
    by_field = {m["field"]: m["index"] for m in mapping if m.get("field") not in ("var", "skip")}
    custom = [(m["index"], core.var_name(m.get("var") or "")) for m in mapping if m.get("field") == "var"]
    custom = [(i, v) for i, v in custom if v]
    if "email" not in by_field:
        raise CampaignError("Выберите столбец с email")
    report = ImportReport()
    rows = cells[1:]
    emails: list[tuple[int, list[str], str]] = []
    seen: set[str] = set()
    for n, row in enumerate(rows, start=2):
        found = table_import._EMAIL.findall(_cell(row, by_field["email"]))
        email = found[0].lower() if found else None
        if not email:
            report.skip(n, _cell(row, by_field["email"]) or None, "bad_email")
            continue
        if email in seen:
            report.skip(n, email, "duplicate")
            continue
        seen.add(email)
        emails.append((n, row, email))
    check = mx_check or mail_domains
    mx = await check({e.split("@", 1)[1] for _, _, e in emails})
    sequence = await db.get(LgSequence, campaign.sequence_id)
    lst = await campaign_list(db, campaign)
    names = set(campaign.variables or [])
    added_ids: list[int] = []
    for n, row, email in emails:
        domain = email.split("@", 1)[1]
        if mx.get(domain) is False:
            report.skip(n, email, "no_mx")
            continue
        site = table_import._first_domain(_cell(row, by_field.get("website"))) if "website" in by_field else None
        if not site and domain not in table_import.FREE_MAIL:
            site = domain
        company_name = _cell(row, by_field.get("company")) or None
        if not site and not company_name:
            report.skip(n, email, "no_company")
            continue
        from app.domains.leadgen.importer import _personal
        phones = [p for p in (table_import.normalize_phone(x) for x in table_import._split(_cell(row, by_field.get("phone"))))
                  if p] if "phone" in by_field else []
        result = await service.upsert_company(db, campaign.workspace_id, service.FindingIn(
            source="import", name=company_name, domain=site, city=_cell(row, by_field.get("city")) or None,
            niche=_cell(row, by_field.get("niche")) or None, phones=phones,
            contacts=[{"kind": "email", "value": email, "is_personal": _personal(email, site)}]), now=now)
        company = result.company
        if company is None:
            report.skip(n, email, "no_company")
            continue
        contact = await db.scalar(select(LgContact).where(LgContact.company_id == company.id, LgContact.kind == "email",
                                                          LgContact.value_norm == email))
        vars_: dict = {}
        for fld, key in STANDARD_VARS.items():
            value = _cell(row, by_field.get(fld)) if fld in by_field else ""
            if value:
                vars_[key] = value[:300]
        for index, name in custom:
            value = _cell(row, index)
            if value:
                vars_[name] = value[:500]
            names.add(name)
        enr = await sequences_enroll(db, sequence, company, campaign, contact, vars_, now)
        if not enr.ok:
            report.skip(n, email, enr.reason or "error")
            continue
        report.added += 1
        added_ids.append(company.id)
    await lists.add_companies(db, lst, added_ids, set(added_ids), now=now)
    campaign.variables = sorted(names)
    free = sum(1 for _, _, e in emails if e.split("@", 1)[1] in table_import.FREE_MAIL)
    if free:
        report.warnings.append(f"{free} адресов на бесплатной почте (gmail, mail.ru…). Если это личные адреса "
                               "сотрудников, а не общий ящик компании — лучше их убрать.")
    unknown = [d for d, ok in mx.items() if ok is None]
    if unknown:
        report.warnings.append(f"Для {len(unknown)} доменов не удалось проверить почтовый сервер — адреса загружены.")
    await db.flush()
    return report.out()


async def sequences_enroll(db, sequence, company, campaign, contact, vars_, now):
    from app.domains.leadgen import sequences
    return await sequences.enroll(db, sequence, company, now=now, campaign=campaign, contact=contact, vars=vars_)


async def add_companies(db: AsyncSession, campaign: LgCampaign, company_ids: list[int], *,
                        now: datetime | None = None) -> dict:
    """Лиды из «Базы компаний»: берём лучшую рабочую почту компании."""
    now = now or service.utcnow()
    sequence = await db.get(LgSequence, campaign.sequence_id)
    lst = await campaign_list(db, campaign)
    report = ImportReport()
    added = []
    for cid in dict.fromkeys(company_ids):
        company = await db.get(LgCompany, cid)
        if company is None or company.workspace_id != campaign.workspace_id:
            report.skip(0, None, "no_company")
            continue
        r = await sequences_enroll(db, sequence, company, campaign, None, None, now)
        if r.ok:
            report.added += 1
            added.append(cid)
        else:
            report.skipped.append({"company_id": cid, "company": company.display_name or company.domain,
                                   "reason": r.reason, "reason_text": REASONS.get(r.reason or "", r.reason)})
    await lists.add_companies(db, lst, added, now=now)
    await db.flush()
    return report.out()


# ---------------------------------------------------------------- отчёт и лиды

LEAD_STATUSES = {"active": "В работе", "paused": "Пауза", "finished": "Цепочка пройдена", "replied": "Ответил",
                 "bounced": "Не доставлено", "stopped": "Остановлен"}


async def stats(db: AsyncSession, campaign: LgCampaign) -> dict:
    by_status = dict((await db.execute(select(LgEnrollment.status, func.count()).where(
        LgEnrollment.campaign_id == campaign.id).group_by(LgEnrollment.status))).all())
    touches = dict((await db.execute(select(LgTouch.status, func.count()).where(
        LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == campaign.sequence_id, LgTouch.channel == "email",
        LgTouch.direction == "out").group_by(LgTouch.status))).all())
    inbound = (await db.execute(select(LgTouch.label, LgTouch.hot_at).where(
        LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == campaign.sequence_id,
        LgTouch.direction == "in"))).all()
    replies = [label for label, _ in inbound if label not in ("auto",)]
    sent = int(touches.get("sent", 0)) + int(touches.get("delivered", 0)) + int(touches.get("replied", 0))
    leads = sum(by_status.values())
    replied = sum(1 for label in replies if label != "unsubscribe")
    return {"leads": leads, "waiting": int(by_status.get("active", 0)), "sent": sent,
            "replied": replied, "reply_rate": round(replied / max(1, leads - int(by_status.get("active", 0)) or leads) * 100, 1) if leads else 0,
            "interested": sum(1 for label in replies if label in ("interested", "question")),
            "hot": sum(1 for _, hot in inbound if hot), "unsubscribed": sum(1 for label in replies if label == "unsubscribe"),
            "bounced": int(touches.get("bounced", 0)), "finished": int(by_status.get("finished", 0)),
            "by_status": by_status}


async def leads(db: AsyncSession, campaign: LgCampaign, *, status: str | None = None, q: str | None = None,
                limit: int = 100, offset: int = 0) -> dict:
    query = (select(LgEnrollment, LgCompany, LgContact).join(LgCompany, LgCompany.id == LgEnrollment.company_id)
             .outerjoin(LgContact, LgContact.id == LgEnrollment.contact_id).where(LgEnrollment.campaign_id == campaign.id))
    if status:
        query = query.where(LgEnrollment.status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(func.lower(LgCompany.display_name).like(like) | func.lower(LgCompany.domain).like(like)
                            | LgContact.value_norm.like(like))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(LgEnrollment.id.desc()).limit(limit).offset(offset))).all()
    from app.domains.leadgen.routes import unicode_email
    boxes = {b.id: b.email for b in (await db.scalars(select(LgMailbox).where(
        LgMailbox.workspace_id == campaign.workspace_id))).all()}
    return {"total": total or 0, "items": [{
        "id": e.id, "company_id": c.id, "company": (e.vars or {}).get("company") or c.display_name or c.domain,
        "domain": c.domain, "email": ct.value_norm if ct else None, "status": e.status,
        "status_name": LEAD_STATUSES.get(e.status, e.status), "step": e.current_step, "next_at": iso(e.next_at),
        "stop_reason": e.stop_reason, "mailbox": unicode_email(boxes[e.mailbox_id]) if e.mailbox_id in boxes else None,
        "vars": e.vars or {}, "crm_deal_id": c.crm_deal_id} for e, c, ct in rows]}


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def row(db: AsyncSession, campaign: LgCampaign, with_stats: bool = True) -> dict:
    seq = await db.get(LgSequence, campaign.sequence_id)
    return {"id": campaign.id, "name": campaign.name, "status": campaign.status, "sequence_id": campaign.sequence_id,
            "window": {**DEFAULT_WINDOW, **(seq.window or {})} if seq else DEFAULT_WINDOW,
            "steps": len(seq.steps or []) if seq else 0, "mailbox_ids": campaign.mailbox_ids or [],
            "variables": campaign.variables or [], "start_at": iso(campaign.start_at), "created_at": iso(campaign.created_at),
            "stats": await stats(db, campaign) if with_stats else None}
