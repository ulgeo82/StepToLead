"""Отчёт клиенту: те же цифры, что в «Результате» и «Аналитике», собранные в понятную историю.

Исключения отчёта («матрёшка» каналов → гипотез → кампаний) действуют только на этот отчёт.
Лиды с отметкой «не считать в аналитику» в CRM не попадают никогда. Итог-заголовок и разбор
гипотез считаются по правилам из цифр (без ИИ), текст плана пишет менеджер в конструкторе.
"""
from collections import defaultdict
from datetime import date, datetime, time, timezone
from statistics import median

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmDeal, CrmStage
from app.models.marketing import AdHypothesis, ClientLead, PortalUser, Project
from app.services.analytics_details import analytics_details
from app.services.result_analytics import (ReportExclude, _change, _ratio, _sum, analytics_exclusions,
                                           result_facts)

SECTIONS = ("summary", "kpi", "funnel", "trend", "channels", "hypotheses", "sales", "plan")


def _pct_text(value: float) -> str:
    return f"{abs(value):.0f}%"


def headline(cur: dict, prev: dict) -> str:
    """Одна фраза-итог из изменений к прошлому периоду: цена лида и число продаж."""
    parts = []
    cpl_change = _change(cur.get("cpl"), prev.get("cpl"))
    if cpl_change is not None and abs(cpl_change) >= 3:
        parts.append(f"заявки {'подешевели' if cpl_change < 0 else 'подорожали'} на {_pct_text(cpl_change)}")
    elif cur.get("leads") is not None and prev.get("leads"):
        leads_change = _change(cur.get("leads"), prev.get("leads"))
        if leads_change is not None and abs(leads_change) >= 3:
            parts.append(f"заявок {'больше' if leads_change > 0 else 'меньше'} на {_pct_text(leads_change)}")
    sales, prev_sales = cur.get("sales"), prev.get("sales")
    if sales is not None and prev_sales is not None and sales != prev_sales:
        diff = int(sales - prev_sales)
        parts.append(f"продаж стало {'больше' if diff > 0 else 'меньше'} на {abs(diff)}")
    elif sales:
        parts.append(f"{int(sales)} продаж")
    if not parts:
        return "Итоги периода"
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def hypothesis_verdicts(campaigns: list, names: dict) -> list:
    """Сработало (есть продажи и выручка покрывает расходы) / не сработало (потратили без продаж) / тестируем."""
    groups = defaultdict(lambda: {"spend": 0.0, "leads": 0, "sales": 0, "revenue": 0.0, "campaigns": 0})
    for row in campaigns:
        if row.get("kind") != "ad" or not row.get("hypothesis_id"):
            continue
        group = groups[row["hypothesis_id"]]
        group["spend"] += float(row.get("spend") or 0)
        group["leads"] += int(row.get("leads") or 0)
        group["sales"] += int(row.get("sales") or 0)
        group["revenue"] += float(row.get("revenue") or 0)
        group["campaigns"] += 1
    result = []
    for hypothesis_id, group in groups.items():
        if group["sales"] and group["revenue"] >= group["spend"]:
            verdict = "worked"
        elif group["spend"] > 0 and not group["sales"] and group["leads"] >= 5:
            verdict = "failed"
        else:
            verdict = "testing"
        result.append({"id": hypothesis_id, "name": names.get(hypothesis_id, f"Гипотеза #{hypothesis_id}"),
                       "verdict": verdict, **group, "cpl": _ratio(group["spend"] or None, group["leads"])})
    order = {"worked": 0, "failed": 1, "testing": 2}
    return sorted(result, key=lambda row: (order[row["verdict"]], -row["revenue"], -row["spend"]))


async def sales_team(db: AsyncSession, project: Project, start: date, end: date, excluded_leads: set) -> dict:
    """Скорость первого ответа, сделки без ответа и результат по менеджерам — за сделки, созданные в периоде."""
    first = datetime.combine(start, time.min, tzinfo=timezone.utc)
    last = datetime.combine(end, time.max, tzinfo=timezone.utc)
    deals = (await db.execute(select(CrmDeal.id, CrmDeal.lead_id, CrmDeal.responsible_user_id, CrmDeal.created_at,
                                     CrmDeal.first_response_at, CrmStage.analytics_type)
                              .join(CrmStage, CrmStage.id == CrmDeal.stage_id)
                              .where(CrmDeal.project_id == project.id, CrmDeal.archived_at.is_(None),
                                     CrmDeal.created_at >= first, CrmDeal.created_at <= last))).all()
    deals = [row for row in deals if row.lead_id not in excluded_leads]
    if not deals:
        return {"deals": 0, "response_minutes": None, "unanswered": 0, "managers": []}
    names = dict((await db.execute(select(PortalUser.id, PortalUser.display_name).where(
        PortalUser.id.in_({row.responsible_user_id for row in deals if row.responsible_user_id})))).all())
    def minutes(row):
        if not row.first_response_at:
            return None
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        answered = row.first_response_at if row.first_response_at.tzinfo else row.first_response_at.replace(tzinfo=timezone.utc)
        return max(0.0, (answered - created).total_seconds() / 60)
    per = defaultdict(lambda: {"deals": 0, "won": 0, "times": []})
    for row in deals:
        item = per[row.responsible_user_id]
        item["deals"] += 1
        item["won"] += row.analytics_type == "WON"
        if minutes(row) is not None:
            item["times"].append(minutes(row))
    all_times = [m for item in per.values() for m in item["times"]]
    managers = [{"name": names.get(user_id, "Без ответственного") if user_id else "Без ответственного",
                 "deals": item["deals"], "won": item["won"],
                 "response_minutes": median(item["times"]) if item["times"] else None,
                 "conversion": item["won"] / item["deals"] * 100 if item["deals"] else None}
                for user_id, item in per.items()]
    managers.sort(key=lambda row: (-row["won"], -row["deals"]))
    unanswered = sum(1 for row in deals if not row.first_response_at and row.analytics_type not in {"WON", "LOST"})
    return {"deals": len(deals), "response_minutes": median(all_times) if all_times else None,
            "unanswered": unanswered, "managers": managers}


async def build_report(db: AsyncSession, project: Project, start: date, end: date,
                       exclude: ReportExclude | None = None, sections=SECTIONS) -> dict:
    granularity = "week" if (end - start).days > 20 else "day"
    facts = await result_facts(db, project, start, end, granularity, exclude=exclude)
    details = await analytics_details(db, project, start, end, facts, exclude)
    cur, prev = facts["current"]["totals"], facts["previous"]["totals"]
    hypotheses = (await db.execute(select(AdHypothesis.id, AdHypothesis.name)
                                   .where(AdHypothesis.project_id == project.id))).all()
    exclusions = await analytics_exclusions(db, project.id)
    total_revenue = cur.get("revenue") or 0
    channels = []
    for source in facts["current"]["sources"]:
        if not any(source.get(field) for field in ("spend", "leads", "sales", "revenue")):
            continue
        channels.append({"id": source["id"], "name": source.get("name"), "group": source.get("group"),
                         "spend": source.get("spend"), "leads": source.get("leads"), "sales": source.get("sales"),
                         "revenue": source.get("revenue"), "cpl": source.get("cpl"), "romi": source.get("romi"),
                         "revenue_share": (source.get("revenue") or 0) / total_revenue * 100 if total_revenue else None})
    channels.sort(key=lambda row: (-(row["revenue"] or 0), -(row["leads"] or 0)))
    first = datetime.combine(start, time.min, tzinfo=timezone.utc)
    last = datetime.combine(end, time.max, tzinfo=timezone.utc)
    flagged = await db.scalar(select(func.count(ClientLead.id)).where(
        ClientLead.project_id == project.id, ClientLead.exclude_from_analytics.is_(True),
        ClientLead.created_at >= first, ClientLead.created_at <= last)) or 0
    changes = {field: _change(cur.get(field), prev.get(field))
               for field in ("spend", "leads", "cpl", "qualified", "sales", "revenue", "average_check", "cac", "romi",
                             "target_share")}
    report = {
        "project": facts["project"], "period": facts["period"], "economics": facts["economics"],
        "sections": [section for section in SECTIONS if section in set(sections)],
        "headline": headline(cur, prev), "totals": cur, "previous": prev, "changes": changes,
        "funnel": facts["funnel"], "trend": facts["current"]["trend"], "granularity": granularity,
        "clicks": details["current"].get("clicks"), "channels": channels,
        "hypotheses": hypothesis_verdicts(details["campaigns"], dict(hypotheses)),
        "campaigns": [row for row in details["campaigns"] if row.get("kind") == "ad"][:30],
        "sales_cycle_days": details.get("sales_cycle_days"), "lost_reasons": details.get("lost_reasons", [])[:5],
        "sales_team": await sales_team(db, project, start, end, exclusions.lead_ids),
        "excluded": {"crm_leads": int(flagged),
                     "channels": len(exclude.channels) if exclude else 0,
                     "hypotheses": len(exclude.hypotheses) if exclude else 0,
                     "campaigns": len(exclude.campaigns) if exclude else 0},
        "built_at": datetime.now(timezone.utc),
    }
    return jsonable_encoder(report)
