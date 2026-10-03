"""CRM → «Импорт из amoCRM»: preview the deals export, then import it with the chosen stage / manager mapping."""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import activity, create_deal_fact, norm_phone, pipeline_for, project_for
from app.api.routes.messaging import can_manage_channels
from app.core.access import check_origin, require_portal_user
from app.core.permissions import require_permission
from app.db import get_db
from app.models.crm import CrmContact, CrmDeal, CrmStage
from app.models.marketing import ClientLead, ClientSale, PortalUser
from app.services import amo_import
from app.services.messaging import project_people

router = APIRouter(prefix="/crm", tags=["crm-import"])
MAX_BYTES = 15 * 1024 * 1024


class ImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline_id: int
    stages: dict[str, int] = Field(default_factory=dict)          # amo stage name → our stage id
    users: dict[str, int | None] = Field(default_factory=dict)    # amo manager name → our user id
    default_user_id: int | None = None
    create_sales: bool = True


async def load(file: UploadFile) -> tuple[list[dict], dict]:
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "Файл больше 15 МБ — выгрузите сделки по периодам")
    try:
        return amo_import.parse(file.filename or "", content)
    except amo_import.ImportError_ as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(422, "Не удалось прочитать файл. Сохраните выгрузку amoCRM в XLSX и попробуйте снова") from None


async def context(db: AsyncSession, user: PortalUser, project_id: int, pipeline_id: int | None):
    require_permission(user, "view_crm")
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Импорт делает руководитель или владелец")
    pipeline = await pipeline_for(db, project.id, pipeline_id)
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id, CrmStage.archived_at.is_(None))
                               .order_by(CrmStage.position))).all()
    people = await project_people(db, project.id)
    return project, pipeline, list(stages), people


@router.post("/projects/{project_id}/import/amocrm/preview")
async def preview(project_id: int, request: Request, file: UploadFile, pipeline_id: int | None = Form(default=None),
                  db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project, pipeline, stages, people = await context(db, user, project_id, pipeline_id)
    deals, columns = await load(file)
    existing = await imported_ids(db, project.id)
    data = amo_import.summary(deals, stages, {uid: u.display_name for uid, u in people.items()})
    data["already_imported"] = sum(1 for d in deals if d["amo_id"] and d["amo_id"] in existing)
    return {**data, "columns": columns, "pipeline_id": pipeline.id,
            "target_stages": [{"id": s.id, "name": s.name, "analytics_type": s.analytics_type} for s in stages],
            "team": [{"id": uid, "name": u.display_name} for uid, u in people.items()]}


async def imported_ids(db: AsyncSession, project_id: int) -> set[str]:
    rows = (await db.scalars(select(CrmDeal.attribution_snapshot).where(CrmDeal.project_id == project_id, CrmDeal.origin == "IMPORT"))).all()
    return {str(r.get("amo_id")) for r in rows if r and r.get("amo_id")}


@router.post("/projects/{project_id}/import/amocrm")
async def run_import(project_id: int, request: Request, file: UploadFile, options: str = Form(...),
                     db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    try:
        opts = ImportOptions.model_validate(json.loads(options))
    except (ValueError, ValidationError):
        raise HTTPException(422, "Некорректные настройки импорта") from None
    project, pipeline, stages, people = await context(db, user, project_id, opts.pipeline_id)
    deals, _ = await load(file)
    stage_by_id = {s.id: s for s in stages}
    if any(sid not in stage_by_id for sid in opts.stages.values()):
        raise HTTPException(422, "Этап не из выбранной воронки")
    fallback = next((s for s in stages if s.analytics_type == "LEAD"), stages[0] if stages else None)
    if fallback is None:
        raise HTTPException(422, "В воронке нет этапов")
    existing = await imported_ids(db, project.id)
    contacts: dict[str, CrmContact] = {}
    result = {"created": 0, "skipped_duplicates": 0, "skipped_no_contact": 0, "contacts_new": 0, "contacts_merged": 0, "sales": 0}

    async def find_contact(deal: dict) -> CrmContact:
        keys = [f"p:{norm_phone(p)}" for p in deal["phones"]] + [f"e:{e}" for e in deal["emails"]]
        for key in keys:
            if key in contacts:
                result["contacts_merged"] += 1
                return contacts[key]
        phones = [norm_phone(p) for p in deal["phones"]]
        found = None
        if phones:
            found = await db.scalar(select(CrmContact).where(CrmContact.project_id == project.id, CrmContact.phone_normalized.in_(phones)).limit(1))
        if not found and deal["emails"]:
            found = await db.scalar(select(CrmContact).where(CrmContact.project_id == project.id, CrmContact.email_normalized.in_(deal["emails"])).limit(1))
        if found:
            result["contacts_merged"] += 1
        else:
            found = CrmContact(workspace_id=project.workspace_id, project_id=project.id, name=deal["contact"], company=deal["company"],
                               phones=[f"+{norm_phone(p)}" for p in deal["phones"]], emails=deal["emails"],
                               phone_normalized=phones[0] if phones else None, email_normalized=(deal["emails"] or [None])[0])
            db.add(found); await db.flush()
            result["contacts_new"] += 1
        for key in keys:
            contacts[key] = found
        return found

    for index, deal in enumerate(deals):
        if deal["amo_id"] and deal["amo_id"] in existing:
            result["skipped_duplicates"] += 1
            continue
        if not deal["phones"] and not deal["emails"]:
            result["skipped_no_contact"] += 1
            continue
        stage = stage_by_id.get(opts.stages.get(deal["stage"]) or 0) or fallback
        owner = opts.users.get(deal["responsible"] or "", opts.default_user_id) if deal["responsible"] else opts.default_user_id
        owner = owner if owner in people else None
        contact = await find_contact(deal)
        row = await create_deal_fact(db, project, contact, pipeline, stage, deal["name"], owner, deal["amount"], None, "IMPORT",
                                     actor=user, created_at=deal["created"])
        row.attribution_snapshot = {"import": "amocrm", "amo_id": deal["amo_id"], **deal["utm"]}
        row.tags = deal["tags"]
        when = deal["closed"] or deal["created"]
        if when:
            row.stage_entered_at = row.last_activity_at = when
        lead = await db.get(ClientLead, row.lead_id)
        if stage.analytics_type in {"WON", "LOST"}:
            row.closed_at = when or row.created_at
        if stage.analytics_type == "LOST":
            lead.status, lead.lost_reason = "lost", f"amoCRM: {deal['stage']}"[:200]
        elif stage.analytics_type in {"QUALIFIED", "WON"}:
            lead.status, lead.quality = "qualified", "target"
            lead.qualified_at = deal["created"] or row.created_at
        if stage.analytics_type == "WON" and opts.create_sales and deal["amount"]:
            db.add(ClientSale(lead_id=lead.id, deal_id=row.id, project_id=project.id, amount=deal["amount"],
                              occurred_at=when or row.created_at, comment="Импорт из amoCRM", confirmed_by_id=user.id))
            lead.status = "won"
            result["sales"] += 1
        amo_ref = f" (сделка {deal['amo_id']})" if deal["amo_id"] else ""
        activity(db, row, user, "AUTOMATION", {"rule": "Импорт", "text": f"Перенесено из amoCRM{amo_ref}, этап «{deal['stage']}»"}, touch=False)
        if deal["note"]:
            activity(db, row, user, "COMMENT_ADDED", {"text": deal["note"]}, touch=False)
        result["created"] += 1
        if deal["amo_id"]:
            existing.add(deal["amo_id"])
        if index % 200 == 199:
            await db.commit()
    await db.commit()
    return result
