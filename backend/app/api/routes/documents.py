"""Commercial offers (КП) and invoices from a deal + the seller's requisites; public link for the client."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import activity, deal_for, project_for
from app.core.access import check_origin, require_portal_user
from app.core.config import settings
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.crm import CrmContact, CrmDeal, CrmDocument
from app.models.marketing import PortalUser, Project
from app.services import documents
from app.services.notifications import direct as notify_direct, flush_telegram

router = APIRouter(prefix="/crm", tags=["crm-documents"])
public_router = APIRouter(prefix="/public", tags=["public-documents"])
STATUSES = {"draft", "sent", "viewed", "paid", "canceled"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def can_edit_requisites(user: PortalUser) -> bool:
    return bool({"manage_settings", "manage_pipeline", "manage_integrations"} & effective_permissions(user))


def requisites(project: Project) -> dict:
    data = dict((project.portal_state or {}).get("requisites") or {})
    return {key: str(data.get(key) or "") for key in documents.REQUISITE_KEYS} | {"vat": str(data.get("vat") or "none")}


def public_url(doc: CrmDocument) -> str:
    return f"{settings.frontend_origin.rstrip('/')}/d/{doc.public_token}"


def doc_json(doc: CrmDocument) -> dict:
    return {"id": doc.id, "deal_id": doc.deal_id, "kind": doc.kind, "kind_name": documents.KINDS[doc.kind],
            "number": doc.number, "title": doc.title, "items": doc.items or [], "total": float(doc.total or 0),
            "note": doc.note, "valid_until": doc.valid_until, "buyer": doc.buyer or {}, "status": doc.status,
            "views": doc.views, "viewed_at": doc.viewed_at, "paid_at": doc.paid_at, "created_at": doc.created_at,
            "url": public_url(doc)}


# --------------------------------------------------------------------------- requisites

class RequisitesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(default="", max_length=240)
    inn: str = Field(default="", max_length=12, pattern=r"^\d{0,12}$")
    kpp: str = Field(default="", max_length=9, pattern=r"^\d{0,9}$")
    ogrn: str = Field(default="", max_length=15, pattern=r"^\d{0,15}$")
    address: str = Field(default="", max_length=400)
    bank: str = Field(default="", max_length=240)
    bik: str = Field(default="", max_length=9, pattern=r"^\d{0,9}$")
    account: str = Field(default="", max_length=20, pattern=r"^\d{0,20}$")
    corr_account: str = Field(default="", max_length=20, pattern=r"^\d{0,20}$")
    director: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=160)
    site: str = Field(default="", max_length=160)
    vat: str = Field(default="none", pattern=r"^(none|0|5|7|10|20|22)$")
    offer_intro: str = Field(default="", max_length=2000)
    offer_terms: str = Field(default="", max_length=2000)
    invoice_terms: str = Field(default="", max_length=1000)
    accent: str = Field(default="", max_length=7, pattern=r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})?$")


@router.get("/projects/{project_id}/requisites")
async def get_requisites(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    project = await project_for(db, user, project_id)
    return {"requisites": requisites(project), "vat_modes": documents.VAT_MODES, "can_manage": can_edit_requisites(user)}


@router.put("/projects/{project_id}/requisites")
async def put_requisites(project_id: int, payload: RequisitesIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_edit_requisites(user):
        raise HTTPException(403, "Реквизиты меняет руководитель или владелец")
    project.portal_state = {**(project.portal_state or {}), "requisites": {k: v.strip() for k, v in payload.model_dump().items()}}
    await db.commit()
    return await get_requisites(project_id, db, user)


# --------------------------------------------------------------------------- documents

class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    qty: float = Field(default=1, gt=0, le=1_000_000)
    unit: str = Field(default="шт.", max_length=16)
    price: float = Field(default=0, ge=0, le=1_000_000_000)
    discount: float = Field(default=0, ge=0, le=100)


class BuyerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=180)
    company: str = Field(default="", max_length=240)
    inn: str = Field(default="", max_length=12, pattern=r"^\d{0,12}$")
    phone: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=160)


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default="offer", pattern=r"^(offer|invoice)$")
    title: str = Field(default="", max_length=220)
    items: list[ItemIn] = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=3000)
    valid_days: int | None = Field(default=14, ge=1, le=365)
    buyer: BuyerIn | None = None
    update_amount: bool = False


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=220)
    items: list[ItemIn] | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=3000)
    valid_days: int | None = Field(default=None, ge=1, le=365)
    buyer: BuyerIn | None = None
    status: str | None = Field(default=None, pattern=r"^(draft|sent|viewed|paid|canceled)$")
    update_amount: bool = False


async def default_buyer(db: AsyncSession, deal: CrmDeal) -> dict:
    contact = await db.get(CrmContact, deal.contact_id)
    if not contact:
        return {}
    return {"name": contact.name or "", "company": contact.company or "", "inn": "",
            "phone": (contact.phones or [""])[0] or "", "email": (contact.emails or [""])[0] or ""}


async def document_for(db: AsyncSession, user: PortalUser, document_id: int) -> tuple[CrmDocument, CrmDeal]:
    doc = await db.get(CrmDocument, document_id)
    if not doc or doc.workspace_id != user.workspace_id:
        raise HTTPException(404, "Документ не найден")
    return doc, await deal_for(db, user, doc.deal_id)


@router.get("/deals/{deal_id}/documents")
async def list_documents(deal_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    deal = await deal_for(db, user, deal_id)
    project = await db.get(Project, deal.project_id)
    rows = (await db.scalars(select(CrmDocument).where(CrmDocument.deal_id == deal.id)
                             .order_by(CrmDocument.created_at.desc(), CrmDocument.id.desc()))).all()
    # Catalog: positions used in this project's documents before (latest price wins).
    catalog: dict[str, dict] = {}
    recent = (await db.scalars(select(CrmDocument).where(CrmDocument.project_id == deal.project_id)
                               .order_by(CrmDocument.id.desc()).limit(200))).all()
    for doc in recent:
        for item in doc.items or []:
            catalog.setdefault(item["name"].casefold(), {"name": item["name"], "unit": item.get("unit") or "шт.", "price": item.get("price") or 0})
    req = requisites(project)
    return {"items": [doc_json(d) for d in rows], "catalog": list(catalog.values())[:300],
            "buyer": await default_buyer(db, deal), "requisites_ready": bool(req["company"]),
            "invoice_ready": bool(req["company"] and req["inn"] and req["account"] and req["bik"]),
            "can_edit_requisites": can_edit_requisites(user)}


@router.post("/deals/{deal_id}/documents", status_code=201)
async def create_document(deal_id: int, payload: DocumentIn, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    project = await db.get(Project, deal.project_id)
    seller = requisites(project)
    if payload.kind == "invoice" and not (seller["company"] and seller["inn"] and seller["account"] and seller["bik"]):
        raise HTTPException(422, "Для счёта заполните реквизиты: название, ИНН, банк, БИК и расчётный счёт")
    items = documents.clean_items([i.model_dump() for i in payload.items])
    if not items:
        raise HTTPException(422, "Добавьте хотя бы одну позицию")
    number = int(await db.scalar(select(func.max(CrmDocument.number)).where(
        CrmDocument.project_id == deal.project_id, CrmDocument.kind == payload.kind)) or 0) + 1
    amount = documents.total(items)
    doc = CrmDocument(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, kind=payload.kind,
                      number=number, title=payload.title.strip() or None, items=items, total=amount,
                      note=payload.note.strip() or None, seller=seller,
                      buyer=payload.buyer.model_dump() if payload.buyer else await default_buyer(db, deal),
                      valid_until=now() + timedelta(days=payload.valid_days) if payload.valid_days else None,
                      public_token=secrets.token_urlsafe(24), status="draft", created_by_id=user.id)
    db.add(doc)
    if payload.update_amount or not deal.amount:
        deal.amount = amount
    await db.flush()
    activity(db, deal, user, "DOCUMENT_CREATED", {"document_id": doc.id, "kind": doc.kind, "number": number,
                                                  "text": f"{documents.KINDS[doc.kind]} № {number} на {documents.fmt(amount)} ₽"})
    await db.commit()
    return doc_json(doc)


@router.put("/documents/{document_id}")
async def update_document(document_id: int, payload: DocumentUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    doc, deal = await document_for(db, user, document_id)
    if payload.items is not None:
        doc.items = documents.clean_items([i.model_dump() for i in payload.items])
        doc.total = documents.total(doc.items)
        if payload.update_amount:
            deal.amount = doc.total
    if payload.title is not None:
        doc.title = payload.title.strip() or None
    if payload.note is not None:
        doc.note = payload.note.strip() or None
    if payload.valid_days:
        doc.valid_until = now() + timedelta(days=payload.valid_days)
    if payload.buyer is not None:
        doc.buyer = payload.buyer.model_dump()
    if payload.status and payload.status != doc.status:
        doc.status = payload.status
        if payload.status == "paid":
            doc.paid_at = now()
            activity(db, deal, user, "DOCUMENT_PAID", {"document_id": doc.id,
                                                       "text": f"{documents.KINDS[doc.kind]} № {doc.number} оплачен"})
    await db.commit()
    return doc_json(doc)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: int, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    doc, _ = await document_for(db, user, document_id)
    if doc.status == "paid":
        raise HTTPException(422, "Оплаченный счёт удалить нельзя")
    await db.delete(doc)
    await db.commit()


async def render_doc(db: AsyncSession, doc: CrmDocument, *, public: bool) -> str:
    deal = await db.get(CrmDeal, doc.deal_id)
    return documents.render(doc, doc.seller or {}, doc.buyer or {}, doc.created_at or now(), public=public,
                            deal_name=deal.name if deal else "")


@router.get("/documents/{document_id}/view", response_class=HTMLResponse)
async def view_document(document_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    doc, _ = await document_for(db, user, document_id)
    return HTMLResponse(await render_doc(db, doc, public=False), headers={"Cache-Control": "private, no-store"})


@public_router.get("/documents/{token}", response_class=HTMLResponse)
async def public_document(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The client's link. The first open by someone outside the portal lands in the deal and pings the manager."""
    doc = await db.scalar(select(CrmDocument).where(CrmDocument.public_token == token[:60])) if len(token) >= 20 else None
    if not doc or doc.status == "canceled":
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Документ не найден</title>"
                            "<p style='font:16px sans-serif;margin:40px'>Документ не найден или отозван.</p>", status_code=404)
    staff = bool(request.cookies.get("stl_portal_session") or request.cookies.get("stl_session"))
    if not staff:
        first = doc.viewed_at is None
        doc.views = int(doc.views or 0) + 1
        doc.viewed_at = doc.viewed_at or now()
        if doc.status in {"draft", "sent"}:
            doc.status = "viewed"
        if first:
            deal = await db.get(CrmDeal, doc.deal_id)
            if deal:
                label = f"{documents.KINDS[doc.kind]} № {doc.number}"
                activity(db, deal, None, "DOCUMENT_VIEWED", {"document_id": doc.id, "text": f"Клиент открыл: {label}"}, touch=False)
                if deal.responsible_user_id:
                    owner = await db.get(PortalUser, deal.responsible_user_id)
                    if owner:
                        notify_direct(db, deal.workspace_id, [owner.id], "Клиент открыл документ", f"{label} · {deal.name}",
                                      {owner.id: owner}, project_id=deal.project_id)
        await db.commit()
        flush_telegram(db)
    return HTMLResponse(await render_doc(db, doc, public=True),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"})
