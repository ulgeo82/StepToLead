"""Shared project-period facts for the client result screen.

Ad platform lead totals are intentionally ignored: a lead is a ClientLead.
Sales and revenue come only from confirmed ClientSale rows unless an explicitly
aggregated source has no individual records for the selected period.
"""
from collections import defaultdict, namedtuple
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import GrowthCalculation
from app.models.marketing import (AdConnection, AdMetricDaily, ClientLead, ClientLeadAttribution,
                                  ClientSale, LeadInboundSource, Project, ProjectEconomics,
                                  ProjectSource, SourceMetricDaily)


def _dt_date(value: datetime) -> date:
    return value.date()


def _sum(values):
    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) if numbers else None


def _ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _change(current, previous):
    return (current - previous) / abs(previous) * 100 if current is not None and previous not in (None, 0) else None


def _bucket(day: date, granularity: str):
    if granularity == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if granularity == "month":
        return day.replace(day=1).isoformat()
    return day.isoformat()



# Analytics read thousands of leads per request. Loading full ORM entities (every column plus
# identity-map bookkeeping) dominated response time, so these reports read only the columns
# they use into light tuples with the same attribute names.
LeadRow = namedtuple("LeadRow", "id created_at qualified_at meeting_at source_id source full_name status quality quality_reason")
AttributionRow = namedtuple("AttributionRow", "connection_id source_id external_campaign_id")


async def load_lead_rows(db: AsyncSession, project_id: int) -> list[tuple[LeadRow, AttributionRow | None]]:
    rows = await db.execute(select(
        ClientLead.id, ClientLead.created_at, ClientLead.qualified_at, ClientLead.meeting_at, ClientLead.source_id,
        ClientLead.source, ClientLead.full_name, ClientLead.status, ClientLead.quality, ClientLead.quality_reason,
        ClientLeadAttribution.id, ClientLeadAttribution.connection_id, ClientLeadAttribution.source_id,
        ClientLeadAttribution.external_campaign_id)
        .outerjoin(ClientLeadAttribution, ClientLeadAttribution.lead_id == ClientLead.id)
        .where(ClientLead.project_id == project_id))
    return [(LeadRow(*r[:10]), AttributionRow(*r[11:]) if r[10] is not None else None) for r in rows]


def manual_source_key(value: str | None) -> str | None:
    name = (value or "").strip()
    return f"manual:{name}" if name and name.casefold() not in {"unknown", "не определено"} else None


async def result_facts(db: AsyncSession, project: Project, start: date, end: date, granularity: str = "day",
                       include_source_trend: bool = False):
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - (end - start)
    earliest = prior_start
    latest = end

    connections = (await db.scalars(select(AdConnection).where(
        AdConnection.project_id == project.id, AdConnection.platform != "telegram_ads"))).all()
    inbound = (await db.scalars(select(LeadInboundSource).where(LeadInboundSource.project_id == project.id))).all()
    custom = (await db.scalars(select(ProjectSource).where(ProjectSource.project_id == project.id))).all()
    sources = {}
    connection_key = {}
    inbound_key = {}
    for row in connections:
        key = f"ad:{row.id}"
        connection_key[row.id] = key
        sources[key] = {"id": key, "name": row.name, "kind": "INTEGRATION", "method": "API",
                        "platform": row.platform, "status": row.status}
    for row in inbound:
        key = f"webhook:{row.id}"
        inbound_key[row.id] = key
        sources[key] = {"id": key, "name": row.name, "kind": "CUSTOM", "method": "WEBHOOK",
                        "platform": None, "status": "connected" if row.active else "inactive"}
    for row in custom:
        key = f"source:{row.id}"
        sources[key] = {"id": key, "name": row.name, "kind": row.kind, "method": row.method,
                        "platform": None, "status": row.status}

    lead_rows = await load_lead_rows(db, project.id)
    lead_source = {}
    for lead, attribution in lead_rows:
        # A verified ad-account attribution takes precedence over the generic website
        # inbound source; otherwise the same paid lead is incorrectly counted as organic site traffic.
        key = connection_key.get(attribution.connection_id) if attribution and attribution.connection_id else None
        if key is None and lead.source_id:
            key = f"source:{lead.source_id}" if f"source:{lead.source_id}" in sources else None
        if key is None and attribution and attribution.source_id:
            key = inbound_key.get(attribution.source_id)
        if key is None:
            key = manual_source_key(lead.source)
            if key and key not in sources:
                sources[key] = {"id": key, "name": lead.source.strip(), "kind": "MANUAL", "method": "MANUAL",
                                "platform": None, "status": "connected"}
        lead_source[lead.id] = key or "unknown"
    sales = (await db.scalars(select(ClientSale).where(ClientSale.project_id == project.id))).all()

    ad_rows = []
    if connections:
        ad_rows = (await db.scalars(select(AdMetricDaily).where(
            AdMetricDaily.connection_id.in_([row.id for row in connections]),
            AdMetricDaily.date >= earliest, AdMetricDaily.date <= latest))).all()
    custom_ids = [row.id for row in custom]
    source_rows = (await db.scalars(select(SourceMetricDaily).where(
        SourceMetricDaily.source_id.in_(custom_ids), SourceMetricDaily.date >= earliest,
        SourceMetricDaily.date <= latest))).all() if custom_ids else []

    economics_link = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    growth = await db.get(GrowthCalculation, economics_link.growth_calculation_id) if economics_link else None
    margin = float(growth.inputs.get("gross_margin")) if growth and growth.inputs.get("gross_margin") is not None else None
    economics = {"average_order_value": growth.inputs.get("average_order_value") if growth else None,
                 "margin": margin, "allowable_cac": float(economics_link.allowable_cac) if economics_link and economics_link.allowable_cac is not None else None,
                 "break_even_sales": growth.results.get("metrics", {}).get("breakEvenCustomersRounded") if growth else None,
                 "active": bool(growth)}

    def build_period(first: date, last: date):
        day_rows = defaultdict(lambda: defaultdict(lambda: {"spend": None, "leads": 0, "qualified": 0, "meetings": 0,
                                                      "sales": 0, "revenue": 0.0}))
        # Presence is tracked independently from zero so an empty project returns nulls.
        presence = defaultdict(lambda: defaultdict(set))
        for row in ad_rows:
            if first <= row.date <= last:
                key = connection_key[row.connection_id]
                cell = day_rows[row.date][key]
                cell["spend"] = (cell["spend"] or 0) + float(row.spend)
                presence[key]["spend"].add(row.date)
        for row in source_rows:
            if first <= row.date <= last:
                key = f"source:{row.source_id}"
                cell = day_rows[row.date][key]
                if row.spend is not None:
                    cell["spend"] = (cell["spend"] or 0) + float(row.spend)
                    presence[key]["spend"].add(row.date)
        for lead, _ in lead_rows:
            key = lead_source[lead.id]
            if first <= _dt_date(lead.created_at) <= last:
                day = _dt_date(lead.created_at)
                day_rows[day][key]["leads"] += 1
                presence[key]["leads"].add(day)
            if lead.qualified_at and first <= _dt_date(lead.qualified_at) <= last:
                day = _dt_date(lead.qualified_at)
                day_rows[day][key]["qualified"] += 1
                presence[key]["qualified"].add(day)
            if project.meeting_enabled and lead.meeting_at and first <= _dt_date(lead.meeting_at) <= last:
                day = _dt_date(lead.meeting_at)
                day_rows[day][key]["meetings"] += 1
                presence[key]["meetings"].add(day)
        for sale in sales:
            if first <= _dt_date(sale.occurred_at) <= last:
                key = lead_source.get(sale.lead_id, "unknown")
                day = _dt_date(sale.occurred_at)
                day_rows[day][key]["sales"] += 1
                if sale.amount is not None:
                    day_rows[day][key]["revenue"] += float(sale.amount)
                presence[key]["sales"].add(day)
                if sale.amount is not None:
                    presence[key]["revenue"].add(day)

        # Historical aggregate rows are used only when this source has no
        # individual lead or sale facts in the selected period.
        aggregated = set()
        for row in source_rows:
            if not first <= row.date <= last:
                continue
            key = f"source:{row.source_id}"
            if presence[key]["leads"] or presence[key]["qualified"] or presence[key]["sales"]:
                continue
            if all(getattr(row, field) is None for field in ("aggregated_leads", "aggregated_qualified", "aggregated_sales", "aggregated_revenue")):
                continue
            aggregated.add(key)
            cell = day_rows[row.date][key]
            for field, column in (("leads", "aggregated_leads"), ("qualified", "aggregated_qualified"),
                                  ("sales", "aggregated_sales"), ("revenue", "aggregated_revenue")):
                value = getattr(row, column)
                if value is not None:
                    cell[field] += float(value) if field == "revenue" else int(value)
                    presence[key][field].add(row.date)

        keys = set(sources) | {key for date_rows in day_rows.values() for key in date_rows}
        quality = defaultdict(lambda: {"target": 0, "non_target": 0})
        for lead, _ in lead_rows:
            if lead.quality in {"target", "non_target"} and first <= _dt_date(lead.created_at) <= last:
                quality[lead_source[lead.id]][lead.quality] += 1
        by_source = []
        for key in sorted(keys):
            rows = [date_rows[key] for date_rows in day_rows.values() if key in date_rows]
            values = {}
            for field in ("spend", "leads", "qualified", "meetings", "sales", "revenue"):
                values[field] = _sum(row[field] for row in rows) if presence[key][field] else None
                if field in {"leads", "qualified", "meetings", "sales"} and values[field] is not None:
                    values[field] = int(values[field])
            if key not in aggregated and values["leads"] is not None:
                for field in ("qualified", "sales"):
                    if values[field] is None:
                        values[field] = 0
                if values["sales"] == 0 and values["revenue"] is None:
                    values["revenue"] = 0
            entry = {**sources.get(key, {"id": key, "name": "Не определено", "kind": "UNKNOWN", "method": None,
                                            "platform": None, "status": None}), **values,
                     "data_mode": "aggregated/manual" if key in aggregated else "individual"}
            entry.update(_derived(values, margin))
            entry.update(quality_fields(quality[key]["target"], quality[key]["non_target"], values["spend"]))
            by_source.append(entry)

        totals = {}
        for field in ("spend", "leads", "qualified", "meetings", "sales", "revenue"):
            totals[field] = _sum(row[field] for row in by_source)
            if field in {"leads", "qualified", "meetings", "sales"} and totals[field] is not None:
                totals[field] = int(totals[field])
        if totals["leads"] is not None:
            for field in ("qualified", "sales"):
                if totals[field] is None:
                    totals[field] = 0
            if totals["sales"] == 0 and totals["revenue"] is None:
                totals["revenue"] = 0
        totals.update(_derived(totals, margin))
        totals.update(quality_fields(sum(q["target"] for q in quality.values()),
                                     sum(q["non_target"] for q in quality.values()), totals["spend"]))

        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        buckets = {}
        for day in days:
            label = _bucket(day, granularity)
            buckets.setdefault(label, {"label": label, "spend": None, "leads": None,
                                       "qualified": None, "meetings": None, "sales": None, "revenue": None})
            row = buckets[label]
            for field in ("spend", "leads", "qualified", "meetings", "sales", "revenue"):
                vals = [value[field] for key, value in day_rows.get(day, {}).items() if day in presence[key][field]]
                if vals:
                    row[field] = (row[field] or 0) + sum(float(value) for value in vals)
        for row in buckets.values():
            row.update(_derived(row, margin))
        period = {"totals": totals, "sources": by_source, "trend": list(buckets.values())}
        if include_source_trend:
            source_trend = {}
            for key in keys:
                keyed_buckets = {label: {"label": label, "spend": None, "leads": None,
                                         "meetings": None, "sales": None, "revenue": None} for label in buckets}
                for day in days:
                    if key not in day_rows.get(day, {}):
                        continue
                    cell = day_rows[day][key]
                    bucket = keyed_buckets[_bucket(day, granularity)]
                    for field in ("spend", "leads", "meetings", "sales", "revenue"):
                        if day in presence[key][field]:
                            bucket[field] = (bucket[field] or 0) + float(cell[field] or 0)
                source_trend[key] = list(keyed_buckets.values())
            period["source_trend"] = source_trend
        return period

    current = build_period(start, end)
    previous = build_period(prior_start, prior_end)
    for field in ("spend", "leads", "qualified", "sales", "revenue", "romi"):
        current["totals"][f"{field}_change"] = _change(current["totals"].get(field), previous["totals"].get(field))
    total = current["totals"]
    funnel = []
    stages = [("Лиды", "leads"), ("Квалифицированные", "qualified")]
    if project.meeting_enabled:
        stages.append(("Встречи", "meetings"))
    stages.append(("Продажи", "sales"))
    for index, (label, field) in enumerate(stages):
        denominator = total.get(stages[index - 1][1]) if index else None
        funnel.append({"label": label, "value": total.get(field),
                       "conversion": _ratio(total.get(field), denominator) * 100 if denominator else None})
    recent = sorted((lead for lead, _ in lead_rows if start <= _dt_date(lead.created_at) <= end),
                    key=lambda lead: (lead.created_at, lead.id), reverse=True)[:5]
    recent_leads = [{"id": lead.id, "name": lead.full_name,
                     "source": sources.get(lead_source[lead.id], {}).get("name", "Не определено"),
                     "created_at": lead.created_at,
                     "status": "lost" if lead.status == "lost" else "sale" if any(s.lead_id == lead.id for s in sales)
                     else "qualified" if lead.qualified_at else "lead",
                     "amount": _sum(s.amount for s in sales if s.lead_id == lead.id)} for lead in recent]
    alerts = []
    p = previous["totals"]
    if total["leads"] is not None and p["leads"] is not None and total["leads"] >= 5 and p["leads"] >= 5:
        if total["cpl"] is not None and p["cpl"] and total["cpl"] > p["cpl"] * 1.2:
            alerts.append({"title": "Стоимость лида выросла", "detail": "CPL выше прошлого периода более чем на 20%.", "href": "/result#channels", "tone": "negative"})
        if total["sales"] is not None and p["sales"] is not None and total["leads"] >= 10 and p["leads"] >= 10:
            cr = _ratio(total["sales"], total["leads"])
            old_cr = _ratio(p["sales"], p["leads"])
            if cr is not None and old_cr and cr < old_cr * 0.8:
                alerts.append({"title": "Конверсия в продажу снизилась", "detail": "Проверьте обработку лидов.", "href": "/leads", "tone": "warning"})
    if economics["allowable_cac"] is not None and total["sales"] is not None and total["sales"] >= 3 and total["cac"] is not None and total["cac"] > economics["allowable_cac"]:
        alerts.append({"title": "CAC выше допустимого", "detail": "Фактическая стоимость продажи превышает предел модели.", "href": "/growth", "tone": "negative"})
    return {"project": {"id": project.id, "name": project.name, "workspace_id": project.workspace_id,
                        "meeting_enabled": project.meeting_enabled},
            "period": {"start": start, "end": end, "previous_start": prior_start, "previous_end": prior_end},
            "current": current, "previous": previous, "funnel": funnel, "economics": economics,
            "recent_leads": recent_leads, "alerts": alerts}


def quality_fields(target: int, non_target: int, spend) -> dict:
    """Lead quality marked by sales: share of target leads among marked ones and cost per target lead."""
    marked = target + non_target
    return {"target": target, "non_target": non_target,
            "target_share": target / marked * 100 if marked else None,
            "cost_per_target": _ratio(spend, target) if target else None}


def _derived(values, margin):
    spend, leads, qualified, sales, revenue = (values.get(key) for key in ("spend", "leads", "qualified", "sales", "revenue"))
    gross_profit = revenue * margin / 100 if revenue is not None and margin is not None else None
    return {"cpl": _ratio(spend, leads), "cpql": _ratio(spend, qualified),
            "cac": _ratio(spend, sales), "average_check": _ratio(revenue, sales),
            "gross_profit": gross_profit,
            "romi": (gross_profit - spend) / spend * 100 if gross_profit is not None and spend not in (None, 0) else None}
