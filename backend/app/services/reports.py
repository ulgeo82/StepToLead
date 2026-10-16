"""Отчёт клиенту: те же цифры, что в «Результате» и «Аналитике», собранные в понятную историю.

Две операции над любой строкой отчёта:
* исключить из расчёта (калькулятор) — лиды, продажи и расходы пропадают из всех итогов этого отчёта;
* скрыть (глазик) — строка не показывается, но её цифры остаются в итогах.
Исключённая гипотеза остаётся видна карточкой «не в итогах», а в конце отчёта перечислено всё, что
не вошло в расчёт, с названиями и суммами — клиент видит, что ничего не спрятано.
Лиды с отметкой «не считать в аналитику» в CRM не попадают никогда. Итог-заголовок и разбор гипотез
считаются по правилам из цифр (без ИИ); текст, план, прогноз и комментарии пишет менеджер.
"""
from collections import defaultdict
from datetime import date, datetime, time, timezone
from statistics import median

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmDeal, CrmStage, CrmTask
from app.models.marketing import AdHypothesis, ClientLead, ClientSale, PortalUser, Project
from app.services.analytics_details import analytics_details
from app.services.result_analytics import (ReportExclude, _change, _derived, _ratio, analytics_exclusions,
                                           result_facts)

SECTIONS = ("summary", "kpi", "funnel", "trend", "channels", "hypotheses", "sales", "plan")
HERO_METRICS = ("revenue", "profit", "romi", "drr", "spend", "leads", "qualified", "cpl", "cac", "average_check")
DEFAULT_HERO = ("revenue", "romi", "spend", "cac")
LOWER_IS_BETTER = {"cpl", "cac", "drr", "spend"}


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
                       "verdict": verdict, "in_totals": True, **group,
                       "cpl": _ratio(group["spend"] or None, group["leads"])})
    order = {"worked": 0, "failed": 1, "testing": 2}
    return sorted(result, key=lambda row: (order[row["verdict"]], -row["revenue"], -row["spend"]))


def _bounds(start: date, end: date):
    return datetime.combine(start, time.min, tzinfo=timezone.utc), datetime.combine(end, time.max, tzinfo=timezone.utc)


def _aware(value: datetime | None):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def sales_team(db: AsyncSession, project: Project, start: date, end: date, excluded_leads: set,
                     excluded_managers: set) -> dict:
    """Скорость первого ответа, сделки без ответа, просроченные задачи и результат по менеджерам.
    Менеджер, снятый в конструкторе (0 — «без ответственного»), не входит ни в таблицу, ни в средние."""
    first, last = _bounds(start, end)
    deals = (await db.execute(select(CrmDeal.id, CrmDeal.lead_id, CrmDeal.responsible_user_id, CrmDeal.created_at,
                                     CrmDeal.first_response_at, CrmStage.analytics_type)
                              .join(CrmStage, CrmStage.id == CrmDeal.stage_id)
                              .where(CrmDeal.project_id == project.id, CrmDeal.archived_at.is_(None),
                                     CrmDeal.created_at >= first, CrmDeal.created_at <= last))).all()
    deals = [row for row in deals if row.lead_id not in excluded_leads]
    overdue_rows = (await db.execute(select(CrmTask.responsible_user_id, func.count(CrmTask.id)).where(
        CrmTask.project_id == project.id, CrmTask.status == "OPEN",
        CrmTask.due_at < datetime.now(timezone.utc)).group_by(CrmTask.responsible_user_id))).all()
    overdue = {user_id: int(count) for user_id, count in overdue_rows}
    user_ids = {row.responsible_user_id for row in deals if row.responsible_user_id} | {u for u in overdue if u}
    names = dict((await db.execute(select(PortalUser.id, PortalUser.display_name).where(
        PortalUser.id.in_(user_ids)))).all()) if user_ids else {}
    everyone = [{"id": user_id or 0, "name": names.get(user_id, "Без ответственного") if user_id else "Без ответственного"}
                for user_id in sorted({row.responsible_user_id for row in deals} | set(overdue), key=lambda v: (v is None, v or 0))]
    deals = [row for row in deals if (row.responsible_user_id or 0) not in excluded_managers]

    def minutes(row):
        if not row.first_response_at:
            return None
        return max(0.0, (_aware(row.first_response_at) - _aware(row.created_at)).total_seconds() / 60)

    per = defaultdict(lambda: {"deals": 0, "won": 0, "times": []})
    for row in deals:
        item = per[row.responsible_user_id]
        item["deals"] += 1
        item["won"] += row.analytics_type == "WON"
        if minutes(row) is not None:
            item["times"].append(minutes(row))
    for user_id in overdue:
        if (user_id or 0) not in excluded_managers:
            per[user_id]
    all_times = [m for item in per.values() for m in item["times"]]
    managers = [{"id": user_id or 0, "name": names.get(user_id, "Без ответственного") if user_id else "Без ответственного",
                 "deals": item["deals"], "won": item["won"], "overdue": overdue.get(user_id, 0),
                 "response_minutes": median(item["times"]) if item["times"] else None,
                 "conversion": item["won"] / item["deals"] * 100 if item["deals"] else None}
                for user_id, item in per.items()]
    managers.sort(key=lambda row: (-row["won"], -row["deals"]))
    unanswered = sum(1 for row in deals if not row.first_response_at and row.analytics_type not in {"WON", "LOST"})
    return {"deals": len(deals), "response_minutes": median(all_times) if all_times else None,
            "unanswered": unanswered, "overdue": sum(row["overdue"] for row in managers),
            "managers": managers, "all_managers": everyone}


def _with_costs(totals: dict, extra: float, margin) -> dict:
    """Итоги с доп. расходами (гонорар, сервисы, подрядчики) и производными: ДРР и прибыль."""
    result = dict(totals)
    if extra:
        result["spend"] = (result.get("spend") or 0) + extra
        result.update(_derived(result, margin))
    spend, revenue = result.get("spend"), result.get("revenue")
    result["drr"] = spend / revenue * 100 if spend is not None and revenue else None
    result["profit"] = (result["gross_profit"] - (spend or 0)) if result.get("gross_profit") is not None else None
    return result


async def _sales_list(db: AsyncSession, project: Project, start: date, end: date, crm_flagged: set) -> list:
    """Крупнейшие продажи периода — в конструкторе их можно снять с расчёта (случайная сделка на 50 млн)."""
    first, last = _bounds(start, end)
    rows = (await db.execute(select(ClientSale.lead_id, func.sum(ClientSale.amount), func.max(ClientSale.occurred_at),
                                    ClientLead.full_name)
                             .join(ClientLead, ClientLead.id == ClientSale.lead_id)
                             .where(ClientSale.project_id == project.id, ClientSale.occurred_at >= first,
                                    ClientSale.occurred_at <= last)
                             .group_by(ClientSale.lead_id, ClientLead.full_name)
                             .order_by(func.sum(ClientSale.amount).desc().nullslast()).limit(15))).all()
    return [{"lead_id": lead_id, "name": name, "amount": float(amount) if amount is not None else None, "date": at}
            for lead_id, amount, at, name in rows if lead_id not in crm_flagged]


async def build_report(db: AsyncSession, project: Project, start: date, end: date,
                       exclude: ReportExclude | None = None, config: dict | None = None) -> dict:
    config = config or {}
    sections = [section for section in SECTIONS if section in set(config.get("sections") or SECTIONS)]
    granularity = config.get("granularity") if config.get("granularity") in {"day", "week", "month"} else (
        "week" if (end - start).days > 20 else "day")
    facts = await result_facts(db, project, start, end, granularity, exclude=exclude)
    details = await analytics_details(db, project, start, end, facts, exclude)
    margin = facts["economics"]["margin"]
    extra_costs = [{"name": item["name"], "amount": float(item["amount"])} for item in config.get("extra_costs") or []
                   if item.get("name") and item.get("amount")]
    extra = sum(item["amount"] for item in extra_costs)
    cur = _with_costs(facts["current"]["totals"], extra, margin)
    prev = _with_costs(facts["previous"]["totals"], extra, margin)
    names = dict((await db.execute(select(AdHypothesis.id, AdHypothesis.name)
                                   .where(AdHypothesis.project_id == project.id))).all())
    crm = await analytics_exclusions(db, project.id)

    # Исключённые гипотезы и кампании считаем отдельно (только с исключениями каналов и сделок),
    # чтобы показать их карточками «не в итогах» и в блоке прозрачности.
    hypotheses = hypothesis_verdicts(details["campaigns"], names)
    outside = {"channels": [], "hypotheses": [], "campaigns": [], "deals": []}
    if exclude and (exclude.hypotheses or exclude.campaigns or exclude.channels):
        plain = await result_facts(db, project, start, end, granularity)
        for source in plain["current"]["sources"]:
            if source["id"] in exclude.channels:
                outside["channels"].append({"name": source.get("name"), "spend": source.get("spend"),
                                            "leads": source.get("leads"), "revenue": source.get("revenue")})
        if exclude.hypotheses or exclude.campaigns:
            wide = ReportExclude(exclude.channels, frozenset(), frozenset(), exclude.leads)
            wide_details = await analytics_details(db, project, start, end, plain, wide)
            for item in hypothesis_verdicts(wide_details["campaigns"], names):
                if item["id"] in exclude.hypotheses:
                    hypotheses.append({**item, "in_totals": False})
                    outside["hypotheses"].append({"name": item["name"], "spend": item["spend"], "leads": item["leads"],
                                                  "revenue": item["revenue"]})
            for row in wide_details["campaigns"]:
                key = (row.get("connection_id"), str(row.get("external_campaign_id")))
                if row.get("kind") == "ad" and key in exclude.campaigns and row.get("hypothesis_id") not in exclude.hypotheses:
                    outside["campaigns"].append({"name": row.get("name"), "spend": row.get("spend"),
                                                 "leads": row.get("leads"), "revenue": row.get("revenue")})
    if exclude and exclude.leads:
        first, last = _bounds(start, end)
        rows = (await db.execute(select(ClientLead.id, ClientLead.full_name, func.sum(ClientSale.amount))
                                 .outerjoin(ClientSale, ClientSale.lead_id == ClientLead.id)
                                 .where(ClientLead.id.in_(exclude.leads), ClientLead.project_id == project.id)
                                 .group_by(ClientLead.id, ClientLead.full_name))).all()
        outside["deals"] = [{"name": name, "revenue": float(amount) if amount is not None else None} for _, name, amount in rows]

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
    first, last = _bounds(start, end)
    flagged = await db.scalar(select(func.count(ClientLead.id)).where(
        ClientLead.project_id == project.id, ClientLead.exclude_from_analytics.is_(True),
        ClientLead.created_at >= first, ClientLead.created_at <= last)) or 0
    changes = {field: _change(cur.get(field), prev.get(field))
               for field in ("spend", "leads", "cpl", "qualified", "sales", "revenue", "average_check", "cac", "romi",
                             "target_share", "drr", "profit")}
    hero = [metric for metric in (config.get("metrics") or DEFAULT_HERO) if metric in HERO_METRICS][:4] or list(DEFAULT_HERO)
    hidden = config.get("hidden") or {}
    budget_plan = config.get("budget_plan")
    report = {
        "project": facts["project"], "period": facts["period"], "economics": facts["economics"],
        "sections": sections, "headline": headline(cur, prev), "totals": cur, "previous": prev, "changes": changes,
        "hero": hero, "extra_costs": extra_costs,
        "budget_plan": {"amount": float(budget_plan), "used": (cur.get("spend") or 0) / float(budget_plan) * 100}
                       if budget_plan else None,
        "funnel": facts["funnel"], "bottleneck": config.get("bottleneck") or "auto",
        "trend": facts["current"]["trend"], "granularity": granularity,
        "clicks": details["current"].get("clicks"), "channels": channels,
        "hypotheses": hypotheses,
        "hidden": {"channels": list(hidden.get("channels") or []), "hypotheses": list(hidden.get("hypotheses") or []),
                   "campaigns": list(hidden.get("campaigns") or [])},
        "sales_cycle_days": details.get("sales_cycle_days"), "lost_reasons": details.get("lost_reasons", [])[:5],
        "sales_team": await sales_team(db, project, start, end, crm.lead_ids | set(exclude.leads if exclude else ()),
                                       {int(value) for value in config.get("exclude_managers") or []}),
        "sales_list": await _sales_list(db, project, start, end, crm.lead_ids),
        "comments": {key: value for key, value in (config.get("comments") or {}).items() if key in SECTIONS and value},
        "forecast": config.get("forecast") or None, "plan_fact": config.get("plan_fact") or [],
        "excluded": {"crm_leads": int(flagged), **outside},
        "built_at": datetime.now(timezone.utc),
    }
    return jsonable_encoder(report)
