"""Отчёты: конструктор (превью с исключениями), шаблон проекта, сохранённые отчёты со снимком и ссылка для клиента."""
import secrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.result import reporting_period, scoped_project
from app.core.access import check_origin
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.report import ClientReport
from app.models.system import AppSetting
from app.services.reports import HERO_METRICS, SECTIONS, build_report
from app.services.result_analytics import parse_report_exclude

router = APIRouter(prefix="/reports", tags=["reports"])
public_router = APIRouter(tags=["reports-public"])
TEMPLATE_KEY = "report_tpl:"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Exclude(Strict):
    channels: list[str] = Field(default_factory=list, max_length=500)
    hypotheses: list[int] = Field(default_factory=list, max_length=500)
    campaigns: list[str] = Field(default_factory=list, max_length=2000)
    leads: list[int] = Field(default_factory=list, max_length=500)


class Hidden(Strict):
    channels: list[str] = Field(default_factory=list, max_length=500)
    hypotheses: list[int] = Field(default_factory=list, max_length=500)
    campaigns: list[str] = Field(default_factory=list, max_length=2000)


class PlanItem(Strict):
    text: str = Field(max_length=300)
    owner: str | None = Field(default=None, max_length=40)


class PlanFactItem(Strict):
    text: str = Field(max_length=300)
    owner: str | None = Field(default=None, max_length=40)
    done: bool = False
    note: str | None = Field(default=None, max_length=300)


class Cost(Strict):
    name: str = Field(max_length=120)
    amount: float = Field(ge=0, le=1_000_000_000)


class Forecast(Strict):
    spend: float | None = Field(default=None, ge=0, le=1_000_000_000)
    leads: int | None = Field(default=None, ge=0, le=10_000_000)
    sales: int | None = Field(default=None, ge=0, le=10_000_000)
    revenue: float | None = Field(default=None, ge=0, le=100_000_000_000)


class Settings(Strict):
    """Всё, что можно сохранить шаблоном проекта (без периода и текстов конкретного месяца)."""
    sections: list[str] = Field(default_factory=lambda: list(SECTIONS), max_length=20)
    exclude: Exclude = Field(default_factory=Exclude)
    hidden: Hidden = Field(default_factory=Hidden)
    metrics: list[str] = Field(default_factory=list, max_length=4)
    granularity: str | None = Field(default=None, pattern="^(day|week|month)$")
    bottleneck: str | None = Field(default=None, max_length=60)
    exclude_managers: list[int] = Field(default_factory=list, max_length=200)
    extra_costs: list[Cost] = Field(default_factory=list, max_length=20)
    budget_plan: float | None = Field(default=None, gt=0, le=1_000_000_000)


class ReportIn(Settings):
    project_id: int
    start: date
    end: date
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=1500)
    plan: list[PlanItem] = Field(default_factory=list, max_length=12)
    plan_fact: list[PlanFactItem] = Field(default_factory=list, max_length=12)
    comments: dict[str, str] = Field(default_factory=dict, max_length=len(SECTIONS))
    forecast: Forecast | None = None


class TemplateIn(Settings):
    project_id: int


async def _build(db, request, payload: ReportIn):
    start, end = reporting_period(payload.start, payload.end)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    require_actor_permission(kind, user, "view_analytics")
    config = payload.model_dump(mode="json")
    config["metrics"] = [metric for metric in config["metrics"] if metric in HERO_METRICS]
    config["comments"] = {key: value.strip()[:600] for key, value in config["comments"].items() if key in SECTIONS and value.strip()}
    forecast = config.get("forecast") or {}
    config["forecast"] = forecast if any(value is not None for value in forecast.values()) else None
    report = await build_report(db, project, start, end, parse_report_exclude(config["exclude"]), config)
    report["summary"] = (payload.summary or "").strip() or None
    report["plan"] = [item.model_dump() for item in payload.plan if item.text.strip()]
    report["plan_fact"] = [item.model_dump() for item in payload.plan_fact if item.text.strip()]
    return project, kind, user, report


@router.post("/preview")
async def preview(payload: ReportIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    _, _, _, report = await _build(db, request, payload)
    return report


@router.get("/template")
async def get_template(request: Request, project_id: int, db: AsyncSession = Depends(get_db)):
    """Шаблон проекта и план из последнего отчёта — для блока «Что обещали в прошлый раз»."""
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    row = await db.get(AppSetting, f"{TEMPLATE_KEY}{project.id}")
    last = await db.scalar(select(ClientReport).where(ClientReport.project_id == project.id)
                           .order_by(ClientReport.created_at.desc()).limit(1))
    return {"template": row.value if row else None,
            "last_plan": (last.config or {}).get("plan", []) if last else [],
            "last_title": last.title if last else None}


@router.put("/template")
async def save_template(payload: TemplateIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    require_actor_permission(kind, user, "view_analytics")
    value = payload.model_dump(mode="json", exclude={"project_id"})
    row = await db.get(AppSetting, f"{TEMPLATE_KEY}{project.id}")
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=f"{TEMPLATE_KEY}{project.id}", value=value))
    await db.commit()
    return {"ok": True}


@router.get("")
async def list_reports(request: Request, project_id: int | None = None, db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    rows = (await db.scalars(select(ClientReport).where(ClientReport.project_id == project.id)
                             .order_by(ClientReport.created_at.desc()).limit(100))).all()
    return [{"id": row.id, "title": row.title, "token": row.token, "created_at": row.created_at,
             "start": (row.config or {}).get("start"), "end": (row.config or {}).get("end"),
             "headline": (row.snapshot or {}).get("headline")} for row in rows]


@router.post("", status_code=201)
async def save_report(payload: ReportIn, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user, report = await _build(db, request, payload)
    title = (payload.title or "").strip() or f"Отчёт {payload.start.strftime('%d.%m.%Y')} — {payload.end.strftime('%d.%m.%Y')}"
    report["title"] = title
    report.pop("sales_list", None)  # список сделок нужен только конструктору — клиенту по ссылке не отдаём
    report["sales_team"].pop("all_managers", None)
    row = ClientReport(workspace_id=project.workspace_id, project_id=project.id, token=secrets.token_urlsafe(24),
                       title=title, config=payload.model_dump(mode="json"), snapshot=report,
                       created_by_id=user.id if kind == "client" else None)
    db.add(row)
    await db.commit()
    return {"id": row.id, "token": row.token, "title": row.title}


async def _owned(db, request, report_id: int) -> ClientReport:
    row = await db.get(ClientReport, report_id)
    if not row:
        raise HTTPException(404, "Отчёт не найден")
    _, kind, user = await scoped_project(db, request, row.project_id)
    require_actor_permission(kind, user, "view_analytics")
    return row


@router.get("/{report_id}")
async def get_report(report_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _owned(db, request, report_id)
    return {"id": row.id, "token": row.token, "title": row.title, "config": row.config, "report": row.snapshot}


@router.delete("/{report_id}", status_code=204)
async def delete_report(report_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await _owned(db, request, report_id)
    await db.delete(row)
    await db.commit()


@public_router.get("/public/report/{token}")
async def public_report(token: str, db: AsyncSession = Depends(get_db)):
    """Ссылка для клиента без входа: только снимок отчёта, никаких других данных проекта."""
    if not 16 <= len(token) <= 48:
        raise HTTPException(404, "Отчёт не найден")
    row = await db.scalar(select(ClientReport).where(ClientReport.token == token))
    if not row:
        raise HTTPException(404, "Отчёт не найден")
    return {"title": row.title, "report": row.snapshot}
