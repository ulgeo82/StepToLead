"""Shared incoming-lead creation for token and provider adapters."""
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmActivity, CrmInbound
from app.models.marketing import LeadInboundReceipt, LeadInboundSource, ProjectSource
from app.models.website import WebsiteSession
from app.services.notifications import flush_telegram, notify
from app.services.project_scope import default_project


async def create_inbound(db: AsyncSession, source: LeadInboundSource, payload,
                         *, site_id: int | None = None, commit: bool = True,
                         allow_raw_contact: bool = False, attribution_extra: dict | None = None) -> dict:
    """attribution_extra is trusted server-side data (e.g. verified_connection_id); never pass user input."""
    project = await default_project(db, source.workspace_id)
    project_id = source.project_id or project.id
    phone_digits = re.sub(r"\D", "", payload.phone or "")
    phone = f"+{phone_digits}" if phone_digits else None
    email = payload.email.lower() if payload.email else None
    if not phone and not email and not payload.contact_consent and not (allow_raw_contact and getattr(payload, "contact", None)):
        raise HTTPException(422, "Заявка должна содержать контакт или согласие на связь")
    if payload.external_id:
        historical = await db.scalar(select(LeadInboundReceipt).where(
            LeadInboundReceipt.source_id == source.id, LeadInboundReceipt.external_id == payload.external_id))
        if historical:
            return {"ok": True, "lead_id": historical.lead_id, "duplicate": True, "status": "ACCEPTED"}
        existing = await db.scalar(select(CrmInbound).where(
            CrmInbound.inbound_source_id == source.id, CrmInbound.external_id == payload.external_id))
        if existing:
            return {"ok": True, "inbound_id": existing.id, "duplicate": True, "status": existing.status}
    mapped = await db.scalar(select(ProjectSource).where(ProjectSource.project_id == project_id,
                                                       ProjectSource.inbound_source_id == source.id))
    if mapped is None:
        mapped = ProjectSource(project_id=project_id, name=source.name, kind="INTEGRATION",
                               method="API", category="Сайт", status="active", inbound_source_id=source.id)
        db.add(mapped)
        await db.flush()
    attribution = {key: getattr(payload, key) for key in (
        "external_campaign_id", "external_ad_id", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "utm_term", "landing_url", "yclid", "gclid") if getattr(payload, key, None)}
    website_session = None
    if payload.website_session_key:
        query = select(WebsiteSession).where(WebsiteSession.project_id == project_id,
                                              WebsiteSession.session_key == payload.website_session_key)
        if site_id is not None:
            query = query.where(WebsiteSession.site_id == site_id)
        website_session = await db.scalar(query)
        if website_session:
            website_session.source_id = mapped.id
            if website_session.connection_id and website_session.external_campaign_id:
                attribution["connection_id"] = website_session.connection_id
            for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "landing_url", "external_campaign_id"):
                if not attribution.get(key):
                    value = getattr(website_session, key, None)
                    if value:
                        attribution[key] = value
    if attribution_extra:
        attribution.update({key: value for key, value in attribution_extra.items() if value is not None})
    inbound = CrmInbound(workspace_id=source.workspace_id, project_id=project_id,
                         website_session_id=website_session.id if website_session else None,
                         inbound_source_id=source.id, source_id=mapped.id,
                         external_id=payload.external_id, name=payload.full_name, phone=phone, email=email,
                         raw_payload=payload.model_dump(mode="json"), attribution=attribution,
                         origin="INTEGRATION", status="NEW")
    db.add(inbound)
    await db.flush()
    db.add(CrmActivity(workspace_id=source.workspace_id, project_id=project_id, inbound_id=inbound.id,
                       actor_name="Система", event_type="INBOUND_CREATED", payload={"source": source.name}))
    extra = getattr(payload, "model_extra", None) or {}
    method, raw_contact = extra.get("contact_method"), extra.get("contact")
    await notify(db, project_id, "new_lead", "Новая заявка", f"{payload.full_name} · {source.name}", details=[
        f"Способ связи: {method}" if method else None,
        f"Контакт: {raw_contact}" if raw_contact else None,
        f"Телефон: {phone}" if phone and not raw_contact else None,
        f"Email: {email}" if email and not raw_contact else None,
        f"Сайт: {extra.get('website')}" if extra.get("website") else None,
        f"Комментарий: {payload.notes[:500]}" if payload.notes else None,
    ])
    if commit:
        await db.commit()
        flush_telegram(db)
    return {"ok": True, "inbound_id": inbound.id, "duplicate": False, "status": "NEW"}
