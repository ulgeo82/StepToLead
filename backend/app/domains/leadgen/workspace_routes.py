"""API «Рабочего места базы»: /api/admin/leadgen/lists/{id}/table|summary|cleanup, правка компании, «на проверку».
Контракт — WORKSPACE.md, раздел 2."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.db import get_db
from app.domains.leadgen import routes as lg_routes
from app.domains.leadgen import source_runner, workspace
from app.models.access import AdminUser

router = APIRouter(prefix="/admin/leadgen", tags=["admin-leadgen-workspace"])


def _err(exc: workspace.WorkspaceError):
    if exc.extra:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail, **exc.extra})
    raise HTTPException(exc.status, exc.detail)


def _filters(request: Request) -> dict:
    return {k: v for k, v in request.query_params.items() if k.startswith("f.")}


@router.get("/lists/{list_id}/table")
async def list_table(list_id: int, request: Request, q: str | None = None, sort: str | None = None,
                     dir: str = "desc", page: int = 1, per_page: int = 100, db: AsyncSession = Depends(get_db),
                     admin: AdminUser = Depends(require_admin)):
    if dir not in ("asc", "desc") or page < 1 or not 1 <= per_page <= 500:
        raise HTTPException(422, "Неверные параметры страницы или сортировки")
    try:
        return await workspace.table(db, lg_routes.workspace_id(), list_id, q=q, sort_key=sort, direction=dir,
                                     page=page, per_page=per_page, params=_filters(request))
    except workspace.WorkspaceError as exc:
        return _err(exc)


@router.get("/lists/{list_id}/summary")
async def list_summary(list_id: int, request: Request, column: str, q: str | None = None,
                       db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    try:
        return await workspace.summary(db, lg_routes.workspace_id(), list_id, column, q=q, params=_filters(request))
    except workspace.WorkspaceError as exc:
        return _err(exc)


class CleanupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: str
    revenue_max: int | None = Field(default=None, gt=0)


@router.post("/lists/{list_id}/cleanup/preview")
async def cleanup_preview(list_id: int, payload: CleanupIn, db: AsyncSession = Depends(get_db),
                          admin: AdminUser = Depends(require_admin)):
    try:
        return await workspace.cleanup(db, lg_routes.workspace_id(), list_id, payload.rule,
                                       payload.model_dump(exclude={"rule"}), apply_it=False)
    except workspace.WorkspaceError as exc:
        return _err(exc)


@router.post("/lists/{list_id}/cleanup")
async def cleanup_apply(list_id: int, payload: CleanupIn, db: AsyncSession = Depends(get_db),
                        admin: AdminUser = Depends(require_admin)):
    try:
        out = await workspace.cleanup(db, lg_routes.workspace_id(), list_id, payload.rule,
                                      payload.model_dump(exclude={"rule"}), apply_it=True)
    except workspace.WorkspaceError as exc:
        return _err(exc)
    await db.commit()
    return out


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=240)
    domain: str | None = Field(default=None, max_length=253)
    inn: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    niche: str | None = Field(default=None, max_length=120)
    personal_line: str | None = Field(default=None, max_length=400)   # строка «Персонализации» (длина — 422 ниже)


@router.patch("/companies/{company_id}")
async def edit_company(company_id: int, payload: EditIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(422, "Нечего менять")
    try:
        c = await workspace.edit(db, lg_routes.workspace_id(), company_id, data)
    except workspace.WorkspaceError as exc:
        return _err(exc)
    await db.commit()
    return lg_routes.company_row(c)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(min_length=1, max_length=10000)
    reason: str | None = Field(default=None, max_length=30)


class ReviewClearIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_ids: list[int] = Field(min_length=1, max_length=10000)


@router.post("/companies/review")
async def review(payload: ReviewIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    n = await workspace.mark_review(db, lg_routes.workspace_id(), payload.company_ids, payload.reason)
    await db.commit()
    return {"updated": n}


@router.post("/companies/review/clear")
async def review_clear(payload: ReviewClearIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    n = await workspace.clear_review(db, lg_routes.workspace_id(), payload.company_ids)
    await db.commit()
    return {"updated": n}


class ListRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str] = Field(min_length=1, max_length=20)
    company_ids: list[int] | None = Field(default=None, max_length=10000)


@router.post("/lists/{list_id}/estimate")
async def list_estimate(list_id: int, payload: ListRunIn, db: AsyncSession = Depends(get_db),
                        admin: AdminUser = Depends(require_admin)):
    try:
        return await source_runner.list_estimate(db, lg_routes.workspace_id(), list_id, steps=payload.steps,
                                                 company_ids=payload.company_ids)
    except source_runner.RunError as exc:
        raise HTTPException(exc.status, exc.detail)


@router.post("/lists/{list_id}/run", status_code=202)
async def list_run(list_id: int, payload: ListRunIn, tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
                   admin: AdminUser = Depends(require_admin)):
    try:
        run = await source_runner.create_list_run(db, lg_routes.workspace_id(), list_id, steps=payload.steps,
                                                  company_ids=payload.company_ids, admin_id=admin.id)
    except source_runner.RunError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.commit()
    tasks.add_task(_background, run.id)
    return {"run_id": run.id}


async def _background(run_id: int) -> None:
    await source_runner.execute_list(run_id, lg_routes.session_factory, lg_routes.job_lock("enrich"))
