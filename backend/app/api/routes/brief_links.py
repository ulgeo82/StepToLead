"""Бриф по ссылке после созвона: без логина, привязан к сделке агентства.

Менеджер в карточке сделки берёт ссылку и выбирает этап «Заполнил бриф». Клиент заполняет полный бриф
(черновик сохраняется сам) и контакты; после «Отправить» сделка переезжает на выбранный этап,
контакт обновляется (ФИО, телефон, почта, Telegram, компания, должность), в сделке — комментарий и задача,
менеджеру — уведомление. Бриф виден в админке «Брифы» и переносится в кабинет клиента как обычно.
"""
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_portal_user
from app.core.config import settings
from app.db import get_db
from app.models.brief import ClientBrief
from app.models.marketing import PortalUser
from app.models.system import AppSetting
from app.services import brief as service

router = APIRouter(tags=["brief-links"])
KEY = "brief_link:"
CONTACT_FIELDS = [("full_name", "ФИО", True), ("position", "Должность", False), ("phone", "Телефон", True),
                  ("email", "Email", False), ("telegram", "Telegram", False), ("company", "Компания", True)]


class LinkIn(BaseModel):
    stage_id: int | None = None


@router.post("/crm/deals/{deal_id}/brief-link")
async def make_link(deal_id: int, payload: LinkIn, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    from app.api.routes.crm import deal_for, pipeline_for, stage_for
    from app.core.permissions import require_permission
    from app.models.marketing import Project
    check_origin(request); require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    if payload.stage_id:
        await stage_for(db, await pipeline_for(db, deal.project_id, deal.pipeline_id), payload.stage_id)
    project = await db.get(Project, deal.project_id)
    project.portal_state = {**(project.portal_state or {}), "brief_link_stage_id": payload.stage_id}
    rows = (await db.scalars(select(AppSetting).where(AppSetting.key.like(KEY + "%")))).all()
    existing = next((r for r in rows if (r.value or {}).get("deal_id") == deal.id and not (r.value or {}).get("submitted_at")), None)
    if existing:
        existing.value = {**existing.value, "stage_id": payload.stage_id}
        token = existing.key[len(KEY):]
    else:
        brief = ClientBrief(workspace_id=None, project_id=None, answers={}, source="link", status="draft")
        db.add(brief); await db.flush()
        token = secrets.token_urlsafe(18)
        db.add(AppSetting(key=KEY + token, value={"deal_id": deal.id, "brief_id": brief.id, "stage_id": payload.stage_id,
                                                  "created_at": datetime.now(timezone.utc).isoformat()}))
    await db.commit()
    return {"url": settings.frontend_origin.rstrip("/") + f"/b/{token}"}


async def _link(db: AsyncSession, token: str):
    row = await db.get(AppSetting, KEY + token) if len(token) >= 16 else None
    if not row:
        raise HTTPException(404, "Ссылка на бриф не найдена. Попросите менеджера прислать новую")
    created = datetime.fromisoformat(row.value["created_at"])
    if created < datetime.now(timezone.utc) - timedelta(days=60):
        raise HTTPException(410, "Ссылка устарела. Попросите менеджера прислать новую")
    brief = await db.get(ClientBrief, row.value["brief_id"])
    return row, brief


async def _contact_prefill(db: AsyncSession, deal_id: int) -> dict:
    from app.models.crm import CrmContact, CrmDeal
    deal = await db.get(CrmDeal, deal_id)
    contact = await db.get(CrmContact, deal.contact_id) if deal else None
    if not contact:
        return {}
    return {"full_name": contact.name if contact.name != "Без имени" else "", "position": contact.position or "",
            "phone": (contact.phones or [""])[0], "email": (contact.emails or [""])[0], "telegram": contact.telegram or "",
            "company": contact.company or ""}


@router.get("/public/brief/{token}")
async def read(token: str, db: AsyncSession = Depends(get_db)):
    row, brief = await _link(db, token)
    contacts = {**await _contact_prefill(db, row.value["deal_id"]), **(row.value.get("contacts") or {})}
    return {"sections": service.BRIEF, "answers": brief.answers or {}, "contacts": contacts,
            "contact_fields": [{"k": k, "t": t, "required": r} for k, t, r in CONTACT_FIELDS],
            "submitted_at": row.value.get("submitted_at"), "privacy_url": settings.brief_privacy_url}


class SaveIn(BaseModel):
    answers: dict = Field(default_factory=dict)
    contacts: dict[str, str] = Field(default_factory=dict)


def _clean_contacts(raw: dict) -> dict:
    return {k: str(raw.get(k) or "").strip()[:180] for k, _, _ in CONTACT_FIELDS}


@router.put("/public/brief/{token}")
async def save(token: str, payload: SaveIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.api.routes.access import rate_limit
    await rate_limit(request, "public_brief_save", 600, 3600)
    row, brief = await _link(db, token)
    brief.answers = service.clean_answers(payload.answers)
    row.value = {**row.value, "contacts": _clean_contacts(payload.contacts)}
    await db.commit()
    return {"ok": True}


@router.post("/public/brief/{token}/submit")
async def submit(token: str, payload: SaveIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.api.routes.access import rate_limit
    from app.api.routes.crm import activity, norm_phone
    from app.models.crm import CrmContact, CrmDeal, CrmStage, CrmStageHistory, CrmTask
    from app.services.notifications import direct, flush_telegram
    await rate_limit(request, "public_brief_submit", 20, 3600)
    row, brief = await _link(db, token)
    contacts = _clean_contacts(payload.contacts)
    missing = [t for k, t, r in CONTACT_FIELDS if r and not contacts[k]]
    if missing:
        raise HTTPException(422, "Заполните контакты: " + ", ".join(missing))
    brief.answers = service.clean_answers(payload.answers)
    brief.status = "review"
    now = datetime.now(timezone.utc)
    deal = await db.get(CrmDeal, row.value["deal_id"])
    if deal:
        contact = await db.get(CrmContact, deal.contact_id)
        if contact:
            contact.name = contacts["full_name"][:180]
            contact.position = contacts["position"] or contact.position
            contact.company = contacts["company"] or contact.company
            if contacts["phone"] and contacts["phone"] not in (contact.phones or []):
                contact.phones = [contacts["phone"], *(contact.phones or [])]
                contact.phone_normalized = norm_phone(contacts["phone"]) or contact.phone_normalized
            if contacts["email"] and contacts["email"].lower() not in [e.lower() for e in contact.emails or []]:
                contact.emails = [contacts["email"], *(contact.emails or [])]
                contact.email_normalized = contacts["email"].lower()
            contact.telegram = contacts["telegram"] or contact.telegram
        stage_id = row.value.get("stage_id")
        stage = await db.get(CrmStage, stage_id) if stage_id else None
        moved = ""
        if stage and stage.pipeline_id == deal.pipeline_id and stage.archived_at is None and deal.stage_id != stage.id \
                and stage.analytics_type not in {"WON", "LOST"} and deal.closed_at is None:
            db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id, to_stage_id=stage.id, actor_id=None))
            deal.stage_id, deal.stage_entered_at = stage.id, now
            moved = f" Сделка перенесена на этап «{stage.name}»."
        url = settings.frontend_origin.rstrip("/") + f"/admin/briefs?brief={brief.id}"
        activity(db, deal, None, "COMMENT_ADDED", {"text": f"📝 Клиент заполнил бриф ({len(brief.answers)} ответов): {url}.{moved}"})
        if deal.responsible_user_id:
            db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                           responsible_user_id=deal.responsible_user_id, title="Разобрать бриф клиента",
                           type_code="FOLLOW_UP", due_at=now + timedelta(hours=4)))
            people = {u.id: u for u in (await db.scalars(select(PortalUser).where(PortalUser.id == deal.responsible_user_id))).all()}
            direct(db, deal.workspace_id, [deal.responsible_user_id], "📝 Клиент заполнил бриф",
                   f"{contacts['company']} · {contacts['full_name']}{moved}", people, project_id=deal.project_id)
    row.value = {**row.value, "contacts": contacts, "submitted_at": now.isoformat()}
    await db.commit()
    flush_telegram(db)
    try:
        from app.services import monitor
        await monitor.send(db, f"📝 Бриф заполнен: {escape(contacts['company'])} · {escape(contacts['full_name'])} · {escape(contacts['phone'])}")
    except Exception:  # noqa: BLE001 — уведомление не должно ломать отправку брифа
        pass
    return {"ok": True}
