"""Еженедельный повтор поиска: запуски с params.repeat=True перезапускаются через 7 дней с теми же ключами.

Так появляются сигналы «новый рекламодатель» и «пропал из рекламы». Работает фоном в backend, раз в час.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import direct_search, service
from app.domains.leadgen.models import LgSourceRun

logger = logging.getLogger(__name__)
INTERVAL = timedelta(days=7)
TICK_SECONDS = 3600


async def due_runs(db: AsyncSession, now: datetime) -> list[LgSourceRun]:
    """Последний запуск каждой серии (fingerprint) с repeat=True, если ему больше 7 дней и он не в работе."""
    rows = (await db.execute(select(LgSourceRun).where(LgSourceRun.source == direct_search.SOURCE)
                             .order_by(LgSourceRun.id.desc()))).scalars().all()
    latest: dict[tuple[int, str], LgSourceRun] = {}
    for run in rows:
        key = (run.workspace_id, (run.params or {}).get("fingerprint") or f"id:{run.id}")
        latest.setdefault(key, run)
    due = []
    for run in latest.values():
        params = run.params or {}
        if not params.get("repeat") or run.status in ("queued", "running"):
            continue
        anchor = service._aware(run.finished_at or run.created_at)
        if anchor and now - anchor >= INTERVAL:
            due.append(run)
    return due


async def schedule_due(db: AsyncSession, now: datetime | None = None) -> list[int]:
    """Создаёт новые запуски-повторы (status=queued) и возвращает их id. Выполняет их вызывающий."""
    now = now or service.utcnow()
    created = []
    for prev in await due_runs(db, now):
        p = prev.params or {}
        run = await direct_search.create_run(db, prev.workspace_id, keywords=p.get("keywords") or [],
                                             region_code=p.get("region_code"), niche=p.get("niche"), city=p.get("city"))
        run.params = {**run.params, "repeat": True, "enrich": p.get("enrich", True), "repeat_of": prev.id}
        created.append(run.id)
    await db.flush()
    return created


async def run_worker() -> None:
    from app.domains.leadgen import routes  # фабрики провайдера и сессий подменяются в тестах
    await asyncio.sleep(120)
    while True:
        try:
            try:
                routes.provider_factory()
            except Exception:  # noqa: BLE001 — источник не настроен: повторять нечего
                await asyncio.sleep(TICK_SECONDS)
                continue
            async with routes.session_factory() as db:
                ids = await schedule_due(db)
                await db.commit()
            for run_id in ids:
                await routes._execute(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("leadgen weekly repeat failed")
        await asyncio.sleep(TICK_SECONDS)
