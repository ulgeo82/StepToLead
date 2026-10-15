from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.result import reporting_period, scoped_project
from app.core.access import check_origin
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.marketing import AdHypothesis
from app.services.analytics_details import analytics_details
from app.services.result_analytics import result_facts

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("")
async def analytics(request: Request, project_id: int | None = None, start: date | None = None,
                    end: date | None = None, granularity: str = "day",
                    db: AsyncSession = Depends(get_db)):
    if granularity not in {"day", "week", "month"}:
        raise HTTPException(422, "Доступна группировка по дням, неделям или месяцам")
    start, end = reporting_period(start, end)
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    facts = await result_facts(db, project, start, end, granularity)
    details = await analytics_details(db, project, start, end, facts)
    return {**facts, "analysis": details,
            "viewer": {"role": "admin" if kind == "admin" else user.role}}


class HypothesisFlagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclude: bool


@router.put("/hypotheses/{hypothesis_id}/exclude")
async def set_hypothesis_flag(hypothesis_id: int, payload: HypothesisFlagIn, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """«Не считать в аналитику» для гипотезы: её лиды, продажи и расходы кампаний не входят в отчёты."""
    check_origin(request)
    row = await db.get(AdHypothesis, hypothesis_id)
    if not row or row.project_id is None:
        raise HTTPException(404, "Гипотеза не найдена")
    project, kind, user = await scoped_project(db, request, row.project_id)
    require_actor_permission(kind, user, "manage_campaigns")
    if project.id != row.project_id:
        raise HTTPException(404, "Гипотеза не найдена")
    row.exclude_from_analytics = payload.exclude
    await db.commit()
    return {"id": row.id, "exclude_from_analytics": row.exclude_from_analytics}
