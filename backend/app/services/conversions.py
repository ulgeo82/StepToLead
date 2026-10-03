"""Offline conversions: CRM results (qualified lead, sale) back to ad systems.

* Yandex Metrica (→ Direct strategies): rows with a Metrica ClientId (from our site tracker reading
  the _ym_uid cookie) or a yclid are uploaded hourly to
  POST https://api-metrika.yandex.net/management/v1/counter/{id}/offline_conversions/upload
  as CSV (ClientId, Yclid, Target, DateTime, Price, Currency). Goals "stl_qualified" / "stl_sale"
  (JavaScript-event goals) are created in the counter on connect.
* VK Ads and anything else: CSV export with phone / email / click ids, uploaded in the cabinet by hand.
"""
import csv
import io
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.crm import CrmContact, CrmDeal, CrmInbound, OfflineConversion
from app.models.marketing import Project
from app.models.website import WebsiteSession

logger = logging.getLogger("uvicorn.error.conversions")
METRIKA = "https://api-metrika.yandex.net"
GOALS = {"qualified": ("stl_qualified", "StepToLead: целевой лид"), "sale": ("stl_sale", "StepToLead: продажа")}
KIND_NAMES = {"qualified": "Целевой лид", "sale": "Продажа"}


class MetrikaError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def metrika_cfg(project: Project) -> dict:
    return dict((project.portal_state or {}).get("metrika") or {})


def save_metrika(project: Project, data: dict | None) -> None:
    state = dict(project.portal_state or {})
    if data is None:
        state.pop("metrika", None)
    else:
        state["metrika"] = data
    project.portal_state = state


async def record(db: AsyncSession, deal: CrmDeal, kind: str, value: float | None = None,
                 occurred_at: datetime | None = None) -> OfflineConversion | None:
    """Remember a CRM result with every identifier we have for the lead. Idempotent per lead and kind."""
    if not deal.lead_id or kind not in GOALS:
        return None
    existing = await db.scalar(select(OfflineConversion).where(OfflineConversion.lead_id == deal.lead_id,
                                                               OfflineConversion.kind == kind))
    if existing:
        if value is not None and existing.status != "sent":
            existing.value = value
        return existing
    inbound = await db.get(CrmInbound, deal.inbound_id) if deal.inbound_id else None
    session = await db.get(WebsiteSession, inbound.website_session_id) if inbound and inbound.website_session_id else None
    attribution, raw = (inbound.attribution or {}) if inbound else {}, (inbound.raw_payload or {}) if inbound else {}
    yclid = attribution.get("yclid") or raw.get("yclid") or (session.click_id if session and session.click_type == "yclid" else None)
    vkclid = raw.get("vkclid") or (session.click_id if session and session.click_type == "vkclid" else None)
    contact = await db.get(CrmContact, deal.contact_id)
    digits = re.sub(r"\D", "", (contact.phones or [""])[0] if contact and contact.phones else "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    row = OfflineConversion(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, lead_id=deal.lead_id,
                            kind=kind, occurred_at=occurred_at or now(), value=value, yclid=yclid,
                            ym_client_id=session.ym_client_id if session else None, vkclid=vkclid,
                            phone=digits or None, email=(contact.emails or [None])[0] if contact else None)
    row.status = "pending" if (row.yclid or row.ym_client_id) else "no_id"
    db.add(row)
    return row


# --------------------------------------------------------------------------- Metrica

async def metrika(method: str, path: str, token: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, f"{METRIKA}{path}", headers={"Authorization": f"OAuth {token}"}, **kwargs)
    except httpx.HTTPError:
        raise MetrikaError("Яндекс Метрика недоступна, повторим позже") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code in {401, 403}:
        raise MetrikaError("Метрика отклонила токен: нужен OAuth-токен с правом изменения счётчика (metrika:write)")
    if response.status_code == 404:
        raise MetrikaError("Счётчик Метрики не найден или нет доступа")
    if response.status_code >= 400:
        message = (data.get("message") or "") if isinstance(data, dict) else ""
        raise MetrikaError(f"Метрика вернула ошибку {response.status_code}{': ' + message[:200] if message else ''}")
    return data if isinstance(data, dict) else {}


async def ensure_goals(counter_id: int, token: str) -> dict:
    goals = (await metrika("GET", f"/management/v1/counter/{counter_id}/goals", token)).get("goals") or []
    result = {}
    for kind, (target, name) in GOALS.items():
        found = next((g for g in goals if any(c.get("url") == target for c in g.get("conditions") or [])), None)
        if not found:
            found = (await metrika("POST", f"/management/v1/counter/{counter_id}/goals", token, json={
                "goal": {"name": name, "type": "action", "conditions": [{"type": "exact", "url": target}]}})).get("goal") or {}
        result[kind] = found.get("id")
    return result


async def connect(project: Project, counter_id: int, token: str) -> dict:
    counter = (await metrika("GET", f"/management/v1/counter/{counter_id}", token)).get("counter") or {}
    goals = await ensure_goals(counter_id, token)
    data = {"counter_id": counter_id, "counter_name": counter.get("name") or counter.get("site"),
            "token": encrypt_secret(token), "goals": goals, "connected_at": now().isoformat(), "last_error": None}
    save_metrika(project, data)
    return data


def metrika_csv(rows: list[OfflineConversion]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ClientId", "Yclid", "Target", "DateTime", "Price", "Currency"])
    for row in rows:
        price = f"{float(row.value):.2f}" if row.value is not None else ""
        writer.writerow([row.ym_client_id or "", row.yclid or "", GOALS[row.kind][0], int(row.occurred_at.timestamp()),
                         price, "RUB" if price else ""])
    return out.getvalue().encode()


async def upload(db: AsyncSession, project: Project) -> int:
    """Send pending conversions of one project to Metrica. Commits."""
    data = metrika_cfg(project)
    token = decrypt_secret(data.get("token")) if data.get("token") else None
    if not data.get("counter_id") or not token:
        return 0
    rows = (await db.scalars(select(OfflineConversion).where(OfflineConversion.project_id == project.id,
                                                             OfflineConversion.status == "pending")
                             .order_by(OfflineConversion.id).limit(5000))).all()
    if not rows:
        return 0
    try:
        result = await metrika("POST", f"/management/v1/counter/{data['counter_id']}/offline_conversions/upload", token,
                               params={"comment": f"StepToLead {now():%Y-%m-%d %H:%M}"},
                               files={"file": ("conversions.csv", metrika_csv(list(rows)), "text/csv")})
    except MetrikaError as exc:
        save_metrika(project, {**data, "last_error": str(exc)[:300]})
        await db.commit()
        return 0
    upload_id = str((result.get("uploading") or {}).get("id") or "")
    for row in rows:
        row.status, row.sent_at, row.upload_id, row.error = "sent", now(), upload_id or None, None
    save_metrika(project, {**data, "last_error": None, "last_upload_at": now().isoformat(), "last_upload_rows": len(rows)})
    await db.commit()
    return len(rows)


async def upload_all(db: AsyncSession) -> None:
    for project in (await db.scalars(select(Project).where(Project.status == "active"))).all():
        if metrika_cfg(project).get("counter_id"):
            try:
                await upload(db, project)
            except Exception:
                await db.rollback()
                logger.exception("metrika upload failed project=%s", project.id)


def export_csv(rows: list[OfflineConversion]) -> str:
    """Universal export for VK Ads «Офлайн-конверсии» and audiences: phone/email plus click ids."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["phone", "email", "event", "datetime", "value", "yclid", "vkclid", "metrica_client_id"])
    for row in rows:
        writer.writerow([row.phone or "", row.email or "", KIND_NAMES[row.kind], row.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                         f"{float(row.value):.2f}" if row.value is not None else "", row.yclid or "", row.vkclid or "",
                         row.ym_client_id or ""])
    return "﻿" + out.getvalue()

