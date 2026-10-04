"""Ad cabinet statistics (Yandex Direct, VK Ads, Meta): kept fresh automatically.

* A lead comes in → it is in the CRM and in every report at once (reports are computed from the database on
  each request). Within a minute the cabinet the lead is attributed to pulls its spend for the last days, so
  CPL / ROMI include today's spend. At most once per LEAD_REFRESH_MINUTES per cabinet (API limits).
* Every EVERY_HOURS hours each cabinet refreshes the last RECENT_DAYS days anyway; the very first sync takes the
  whole history. A cabinet in error is retried every EVERY_HOURS hours.
Avito has its own worker (services/avito_leads.py); Yandex Maps / 2GIS have no API.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select

from app.models.marketing import AdConnection, ClientLeadAttribution, Project

logger = logging.getLogger("uvicorn.error.ad_sync")
PLATFORMS = ("yandex", "vk_ads", "meta")
EVERY_HOURS = 3
RECENT_DAYS = 7
LEAD_DAYS = 2
LEAD_REFRESH_MINUTES = 15
TICK_SECONDS = 60


def now() -> datetime:
    return datetime.now(timezone.utc)


def _base():
    return select(AdConnection).join(Project, Project.id == AdConnection.project_id).where(
        AdConnection.platform.in_(PLATFORMS), Project.status == "active")


async def due(db) -> list[int]:
    """Scheduled refresh: not synced for EVERY_HOURS; failed cabinets wait EVERY_HOURS after the last attempt."""
    edge = now() - timedelta(hours=EVERY_HOURS)
    rows = (await db.scalars(_base().with_only_columns(AdConnection.id).where(
        AdConnection.status.in_(("connected", "error")),
        or_(AdConnection.last_synced_at.is_(None), AdConnection.last_synced_at < edge),
        or_(AdConnection.last_checked_at.is_(None), AdConnection.status == "connected", AdConnection.last_checked_at < edge))
        .order_by(AdConnection.last_synced_at.asc().nulls_first()).limit(20))).all()
    return list(rows)


async def lead_driven(db) -> list[int]:
    """Cabinets that got a lead after their last sync and were not refreshed in the last LEAD_REFRESH_MINUTES."""
    edge = now() - timedelta(minutes=LEAD_REFRESH_MINUTES)
    newer_lead = exists().where(ClientLeadAttribution.connection_id == AdConnection.id,
                                ClientLeadAttribution.created_at > AdConnection.last_synced_at)
    rows = (await db.scalars(_base().with_only_columns(AdConnection.id).where(
        AdConnection.status == "connected", AdConnection.last_synced_at.is_not(None),
        AdConnection.last_synced_at < edge, newer_lead).limit(20))).all()
    return list(rows)


async def _sync(ids: list[int], recent_days) -> int:
    from app.api.routes.marketing import sync_core
    from app.db import SessionLocal
    done = 0
    for connection_id in ids:
        async with SessionLocal() as db:
            row = await db.get(AdConnection, connection_id)
            days = recent_days if row and row.last_synced_at else None  # first sync: full history
            try:
                await sync_core(db, connection_id, days)
                done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row = await db.get(AdConnection, connection_id)
                if row:  # remember the attempt so a broken cabinet is retried only every few hours
                    row.last_checked_at = now(); await db.commit()
                logger.warning("auto sync failed connection=%s: %s", connection_id, str(exc)[:200])
    return done


async def run_once() -> int:
    from app.db import SessionLocal
    async with SessionLocal() as db:
        fresh = await lead_driven(db)
        scheduled = [i for i in await due(db) if i not in fresh]
    return await _sync(fresh, LEAD_DAYS) + await _sync(scheduled, RECENT_DAYS)


async def run_worker() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ad sync iteration failed")
        await asyncio.sleep(TICK_SECONDS)
