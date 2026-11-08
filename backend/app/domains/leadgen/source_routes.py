"""API «Источники компаний»: /api/admin/leadgen/sources/* и прогресс запуска. Контракт — SOURCES.md."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.db import get_db
from app.domains.leadgen import routes as lg_routes
from app.domains.leadgen import source_runner, source_settings
from app.domains.leadgen.models import LgSourceRun
from app.domains.leadgen.sources import REGISTRY, short_error
from app.models.access import AdminUser
from app.services import progress

router = APIRouter(prefix="/admin/leadgen", tags=["admin-leadgen-sources"])
CHECK_TIMEOUT = 60


def _http(exc: source_runner.RunError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


async def _pair(db: AsyncSession, code: str):
    try:
        return await source_runner.adapter_row(db, code)
    except source_runner.RunError as exc:
        raise _http(exc)


@router.get("/sources")
async def list_sources(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    rows = await source_settings.ensure(db)
    await db.commit()
    return {"items": [source_runner.source_view(a, rows[c]) for c, a in REGISTRY.items()]}


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    settings: dict | None = None


@router.patch("/sources/{code}")
async def patch_source(code: str, payload: SourcePatch, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    adapter, row = await _pair(db, code)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.settings:
        try:
            source_settings.apply(adapter, row, payload.settings)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)[:200] or "Неверное значение настройки")
    await db.commit()
    return source_runner.source_view(adapter, row)


@router.post("/sources/{code}/check")
async def check_source(code: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    import asyncio
    adapter, row = await _pair(db, code)
    settings = source_settings.plain(adapter, row.settings)
    try:
        ok, message = await asyncio.wait_for(adapter.healthcheck(settings, source_settings.context()), CHECK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        ok, message = False, short_error(exc)
    source_settings.mark(row, source_runner.service.utcnow(), None if ok else message)
    await db.commit()
    return {"ok": ok, "message": message}


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: dict | None = None
    target: dict | None = None
    list_name: str | None = Field(default=None, max_length=180)


@router.post("/sources/{code}/estimate")
async def estimate_source(code: str, payload: RunIn, db: AsyncSession = Depends(get_db),
                          admin: AdminUser = Depends(require_admin)):
    try:
        return await source_runner.estimate(db, lg_routes.workspace_id(), code, query=payload.query,
                                            target=payload.target)
    except source_runner.RunError as exc:
        raise _http(exc)


@router.post("/sources/{code}/run", status_code=202)
async def run_source(code: str, payload: RunIn, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
                     admin: AdminUser = Depends(require_admin)):
    try:
        run = await source_runner.create_run(db, lg_routes.workspace_id(), code, query=payload.query,
                                             target=payload.target, list_name=payload.list_name, admin_id=admin.id)
    except source_runner.RunError as exc:
        raise _http(exc)
    await db.commit()
    kind = REGISTRY[code].kind
    tasks.add_task(_background, run.id, "enrich" if kind == "enricher" else "search")
    return {"run_id": run.id}


async def _background(run_id: int, lock_kind: str) -> None:
    await source_runner.execute(run_id, lg_routes.session_factory, lg_routes.job_lock(lock_kind))


@router.get("/runs/{run_id}/progress")
async def run_progress(run_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    run = await db.get(LgSourceRun, run_id)
    if run is None or run.workspace_id != lg_routes.workspace_id():
        raise HTTPException(404, "Запуск не найден")
    return progress.get(source_runner.run_key(run_id)) or {"state": "idle"}
