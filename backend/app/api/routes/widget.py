"""Site widget: a floating «call me back» form and messenger buttons on the client's site (public/stl-widget.js).

Leads land in the CRM through the site's own inbound source (auto-accepted, round-robin), with UTM / click ids and the
analytics session when the visitor agreed to analytics. The public endpoints accept only the site's own origin.
"""
import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.access import rate_limit
from app.api.routes.ads import require_manage_integrations
from app.api.routes.portal import InboundLead
from app.api.routes.result import scoped_project
from app.core.access import check_origin, token_digest
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.marketing import LeadInboundSource
from app.models.website import WebsiteSite
from app.services.inbound_lead import create_inbound
from app.services.notifications import flush_telegram

router = APIRouter(prefix="/website", tags=["website-widget"])
DEFAULTS = {"enabled": False, "title": "Перезвоним за 5 минут", "text": "Оставьте номер — ответим на вопросы и посчитаем стоимость.",
            "button": "Перезвоните мне", "color": "#006BFD", "position": "right", "delay_sec": 0, "callback": True,
            "whatsapp": "", "telegram": "", "max": "", "phone": "", "privacy_url": "", "success": "Спасибо! Скоро перезвоним."}
PUBLIC_KEYS = tuple(DEFAULTS)
URL = r"^(https://[^\s<>\"']{3,300})?$"


class WidgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    title: str = Field(default=DEFAULTS["title"], max_length=60)
    text: str = Field(default="", max_length=200)
    button: str = Field(default=DEFAULTS["button"], min_length=2, max_length=30)
    color: str = Field(default="#006BFD", pattern=r"^#[0-9a-fA-F]{6}$")
    position: str = Field(default="right", pattern=r"^(right|left)$")
    delay_sec: int = Field(default=0, ge=0, le=120)
    callback: bool = True
    whatsapp: str = Field(default="", max_length=20, pattern=r"^\+?[\d\s()-]{0,20}$")
    telegram: str = Field(default="", max_length=40, pattern=r"^@?[A-Za-z0-9_]{0,40}$")
    max: str = Field(default="", max_length=300, pattern=URL)
    phone: str = Field(default="", max_length=24, pattern=r"^\+?[\d\s()-]{0,24}$")
    privacy_url: str = Field(default="", max_length=300, pattern=URL)
    success: str = Field(default=DEFAULTS["success"], max_length=160)


class WidgetLead(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(default="", max_length=120)
    phone: str = Field(min_length=5, max_length=32)
    comment: str = Field(default="", max_length=600)
    consent: bool
    page: str = Field(default="", max_length=1500)
    website: str = Field(default="", max_length=200)  # honeypot: humans never see this field
    website_session_key: str | None = Field(default=None, min_length=16, max_length=64)
    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=500)
    utm_content: str | None = Field(default=None, max_length=500)
    utm_term: str | None = Field(default=None, max_length=500)
    click_id: str | None = Field(default=None, max_length=255)
    click_type: str | None = Field(default=None, pattern=r"^(yclid|gclid|vkclid)$")


def widget_config(site: WebsiteSite) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (site.widget or {}).items() if k in DEFAULTS}}


def public_config(site: WebsiteSite) -> dict:
    conf = widget_config(site)
    data = {k: conf[k] for k in PUBLIC_KEYS}
    data["whatsapp"] = re.sub(r"\D", "", conf["whatsapp"])
    data["telegram"] = conf["telegram"].lstrip("@")
    data["phone"] = re.sub(r"[^\d+]", "", conf["phone"])
    return data


def cors(site: WebsiteSite, **extra) -> dict:
    return {"Access-Control-Allow-Origin": site.origin, "Vary": "Origin", **extra}


async def public_site(db: AsyncSession, key: str, request: Request) -> WebsiteSite:
    site = await db.scalar(select(WebsiteSite).where(WebsiteSite.public_key == key[:64], WebsiteSite.active.is_(True)))
    if not site or not widget_config(site)["enabled"]:
        raise HTTPException(404, "Виджет не найден")
    if request.headers.get("origin") != site.origin:
        raise HTTPException(403, "Адрес сайта не совпадает")
    return site


async def site_for(db: AsyncSession, request: Request, site_id: int, manage: bool):
    site = await db.get(WebsiteSite, site_id)
    if not site:
        raise HTTPException(404, "Сайт не найден")
    project, kind, user = await scoped_project(db, request, site.project_id)
    if manage:
        await require_manage_integrations(db, project, kind, user)
    else:
        require_actor_permission(kind, user, "view_analytics")
    return site, project


@router.get("/sites/{site_id}/widget")
async def get_widget(site_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    site, _ = await site_for(db, request, site_id, manage=False)
    return {"widget": widget_config(site), "public_key": site.public_key, "origin": site.origin}


@router.put("/sites/{site_id}/widget")
async def put_widget(site_id: int, payload: WidgetIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    site, project = await site_for(db, request, site_id, manage=True)
    if payload.enabled and not (payload.callback or payload.whatsapp or payload.telegram or payload.max or payload.phone):
        raise HTTPException(422, "Включите форму обратного звонка или укажите хотя бы один мессенджер")
    current = dict(site.widget or {})
    source = await db.get(LeadInboundSource, current.get("source_id")) if current.get("source_id") else None
    if source is None:
        token = secrets.token_urlsafe(32)
        source = LeadInboundSource(workspace_id=project.workspace_id, project_id=project.id, name=f"Виджет: {site.name}"[:180],
                                   token_hash=token_digest(token), token_prefix=token[:12], active=True, auto_assign=True,
                                   auto_accept=True)
        db.add(source)
        await db.flush()
    site.widget = {**{k: v.strip() if isinstance(v, str) else v for k, v in payload.model_dump().items()}, "source_id": source.id}
    await db.commit()
    return {"widget": widget_config(site), "public_key": site.public_key, "origin": site.origin}


@router.get("/widget/{public_key}")
async def widget_public(public_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    site = await public_site(db, public_key, request)
    return Response(json.dumps(public_config(site), ensure_ascii=False), media_type="application/json",
                    headers=cors(site, **{"Cache-Control": "public, max-age=300"}))


@router.post("/widget/{public_key}/lead", status_code=201)
async def widget_lead(public_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The widget posts text/plain JSON (no CORS preflight), like the collector."""
    site = await public_site(db, public_key, request)
    await rate_limit(request, f"widget:{site.id}", 30, 600)
    raw = await request.body()
    if len(raw) > 8192:
        raise HTTPException(413, "Слишком большая заявка")
    try:
        data = WidgetLead.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "Проверьте номер телефона") from None
    if not data.consent:
        raise HTTPException(422, "Нужно согласие на обработку персональных данных")
    digits = re.sub(r"\D", "", data.phone)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        raise HTTPException(422, "Проверьте номер телефона")
    headers = cors(site)
    if data.website:  # bot filled the hidden field: pretend success, store nothing
        return Response(json.dumps({"ok": True}), status_code=201, media_type="application/json", headers=headers)
    conf = widget_config(site)
    source = await db.get(LeadInboundSource, (site.widget or {}).get("source_id"))
    if not source or not source.active:
        raise HTTPException(404, "Виджет не найден")
    page = data.page if data.page.startswith(site.origin) else None
    notes = "Заявка из виджета на сайте: просит перезвонить" + (f"\nКомментарий: {data.comment.strip()}" if data.comment.strip() else "")
    lead = {"full_name": data.name.strip() if len(data.name.strip()) >= 2 else "Клиент с сайта", "phone": f"+{digits}",
            "notes": notes, "contact_consent": True, "landing_url": page, "website_session_key": data.website_session_key,
            "source": "Виджет на сайте", **{k: getattr(data, k) for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")}}
    if data.click_id and data.click_type in {"yclid", "gclid"}:
        lead[data.click_type] = data.click_id
    elif data.click_id and data.click_type == "vkclid":
        lead["vkclid"] = data.click_id
    await create_inbound(db, source, InboundLead.model_validate(lead), site_id=site.id)
    flush_telegram(db)
    return Response(json.dumps({"ok": True, "message": conf["success"]}, ensure_ascii=False), status_code=201,
                    media_type="application/json", headers=headers)
