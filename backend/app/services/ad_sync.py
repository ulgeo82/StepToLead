"""Automatic refresh of ad cabinet statistics (Yandex Direct, VK Ads, Meta).

Leads come into the CRM at once (webhooks, chats, calls); spend and clicks of API cabinets are pulled here every
few hours so CPL and ROMI stay current without pressing «Синхронизировать». Avito has its own worker
(services/avito_leads.py); Yandex Maps / 2GIS have no API.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.models.marketing import AdConnection, Project

logger = logging.getLogger("uvicorn.error.ad_sync")
PLATFORMS = ("yandex", "vk_ads", "meta")
EVERY_HOURS = 3
CHECK_SECONDS = 600


async def due(db) -> list[int]:
    edge = datetime.now(timezone.utc) - timedelta(hours=EVERY_HOURS)
    rows = (await db.scalars(select(AdConnection.id).join(Project, Project.id == AdConnection.project_id).where(
        AdConnection.platform.in_(PLATFORMS), AdConnection.status.in_(("connected", "error")), Project.status == "active",
        or_(AdConnection.last_synced_at.is_(None), AdConnection.last_synced_at < edge),
        or_(AdConnection.last_checked_at.is_(None), AdConnection.status == "connected", AdConnection.last_checked_at < edge))
        .order_by(AdConnection.last_synced_at.asc().nulls_first()).limit(20))).all()
    return list(rows)


async def run_once() -> int:
    from app.api.routes.marketing import sync_core
    from app.db import SessionLocal
    async with SessionLocal() as db:
        ids = await due(db)
    done = 0
    for connection_id in ids:
        async with SessionLocal() as db:
            try:
                await sync_core(db, connection_id)
                done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row = await db.get(AdConnection, connection_id)
                if row:  # remember the attempt so a broken cabinet is retried only every few hours
                    row.last_checked_at = datetime.now(timezone.utc); await db.commit()
                logger.warning("auto sync failed connection=%s: %s", connection_id, str(exc)[:200])
    return done


async def run_worker() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ad sync iteration failed")
        await asyncio.sleep(CHECK_SECONDS)
