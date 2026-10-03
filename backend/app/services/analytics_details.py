"""Project-scoped drilldowns layered on the same facts used by /result."""
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesis,
                                  AdHypothesisCampaign, AdMetricDaily, ClientLead,
                                  ClientLeadAttribution, ClientLeadEvent, ClientSale, Project,
                                  SourceMetricDaily)
from app.services.result_analytics import _derived, _ratio, _sum, quality_fields


def defaultdict_count(values) -> dict:
    counts = defaultdict(int)
    for value in values:
        counts[value] += 1
    return counts


def _pct(numerator, denominator):
    value = _ratio(numerator, denominator)
    return value * 100 if value is not None else None


def _rollup(rows):
    if not rows:
        return {"impressions": None, "clicks": None}
    return {"impressions": sum(int(row.impressions or 0) for row in rows),
            "clicks": sum(int(row.clicks or 0) for row in rows)}


def _exposure_by_source(connections, source_rows, ad_rows):
    result = {}
    for connection in connections:
        result[f"ad:{connection.id}"] = _rollup([row for row in ad_rows if row.connection_id == connection.id])
    for row in source_rows:
        key = f"source:{row.source_id}"
        entry = result.setdefault(key, {"impressions": None, "clicks": None})
        for field in ("impressions", "clicks"):
            value = getattr(row, field)
            if value is not None:
                entry[field] = (entry[field] or 0) + int(value)
    return result


def _period_exposure(first, last, connections, source_rows, ad_rows, result_sources, totals):
    exposures = _exposure_by_source(connections,
                                    [row for row in source_rows if first <= row.date <= last],
                                    [row for row in ad_rows if first <= row.date <= last])
    channel_rows = []
    for source in result_sources:
        exposure = exposures.get(source["id"], {"impressions": None, "clicks": None})
        clicks = exposure["clicks"]
        impressions = exposure["impressions"]
        channel_rows.append({**source, **exposure,
                             "cpc": _ratio(source["spend"], clicks),
                             "ctr": _pct(clicks, impressions),
                             "click_to_lead": _pct(source["leads"], clicks),
                             "lead_to_qualified": _pct(source["qualified"], source["leads"]),
                             "qualified_to_sale": _pct(source["sales"], source["qualified"])})
    total_impressions = _sum(row["impressions"] for row in channel_rows)
    total_clicks = _sum(row["clicks"] for row in channel_rows)
    if total_impressions is not None:
        total_impressions = int(total_impressions)
    if total_clicks is not None:
        total_clicks = int(total_clicks)
    # Only click-capable sources participate in click→lead conversion.
    click_leads = _sum(row["leads"] for row in channel_rows if row["clicks"] is not None)
    click_spend = _sum(row["spend"] for row in channel_rows if row["clicks"] is not None)
    return {"impressions": total_impressions, "clicks": total_clicks,
            "cpc": _ratio(click_spend, total_clicks),
            "ctr": _pct(total_clicks, total_impressions),
            "click_to_lead": _pct(click_leads, total_clicks),
            "lead_to_qualified": _pct(totals["qualified"], totals["leads"]),
            "qualified_to_sale": _pct(totals["sales"], totals["qualified"]),
            "lead_to_sale": _pct(totals["sales"], totals["leads"]),
            "sources": channel_rows}


async def analytics_details(db: AsyncSession, project: Project, start: date, end: date, result: dict):
    connections = (await db.scalars(select(AdConnection).where(AdConnection.project_id == project.id)
                                    .order_by(AdConnection.name))).all()
    connection_map = {row.id: row for row in connections}
    connection_ids = list(connection_map)
    hypotheses = (await db.scalars(select(AdHypothesis).where(AdHypothesis.project_id == project.id)
                                   .order_by(AdHypothesis.name))).all()
    hypothesis_map = {row.id: row for row in hypotheses}
    assignment_rows = (await db.scalars(select(AdHypothesisCampaign).where(
        AdHypothesisCampaign.connection_id.in_(connection_ids)))).all() if connection_ids else []
    assignments = {(row.connection_id, row.external_campaign_id): row.hypothesis_id
                   for row in assignment_rows if row.hypothesis_id in hypothesis_map}

    previous_start = date.fromisoformat(str(result["period"]["previous_start"]))
    ad_rows = (await db.scalars(select(AdMetricDaily).where(
        AdMetricDaily.connection_id.in_(connection_ids), AdMetricDaily.date >= previous_start,
        AdMetricDaily.date <= end))).all() if connection_ids else []
    custom_ids = [int(source["id"].split(":")[1]) for source in result["current"]["sources"]
                  if source["id"].startswith("source:")]
    source_rows = (await db.scalars(select(SourceMetricDaily).where(
        SourceMetricDaily.source_id.in_(custom_ids), SourceMetricDaily.date >= previous_start,
        SourceMetricDaily.date <= end))).all() if custom_ids else []
    current = _period_exposure(start, end, connections, source_rows, ad_rows,
                               result["current"]["sources"], result["current"]["totals"])
    previous_end = date.fromisoformat(str(result["period"]["previous_end"]))
    previous = _period_exposure(previous_start, previous_end, connections, source_rows, ad_rows,
                                result["previous"]["sources"], result["previous"]["totals"])

    campaign_rows = (await db.scalars(select(AdCampaignMetricDaily).where(
        AdCampaignMetricDaily.connection_id.in_(connection_ids),
        AdCampaignMetricDaily.date >= start, AdCampaignMetricDaily.date <= end))).all() if connection_ids else []
    lead_rows = (await db.execute(select(ClientLead, ClientLeadAttribution)
                 .outerjoin(ClientLeadAttribution, ClientLeadAttribution.lead_id == ClientLead.id)
                 .where(ClientLead.project_id == project.id))).all()
    attribution = {}
    campaign_leads = defaultdict(list)
    for lead, attr in lead_rows:
        if not attr or attr.connection_id not in connection_map or not attr.external_campaign_id:
            continue
        key = (attr.connection_id, attr.external_campaign_id)
        attribution[lead.id] = key
        campaign_leads[key].append(lead)
    sales = (await db.scalars(select(ClientSale).where(ClientSale.project_id == project.id))).all()
    lead_created = {lead.id: lead.created_at for lead, _ in lead_rows}
    cycle_days = [max(0, (sale.occurred_at - lead_created[sale.lead_id]).total_seconds() / 86400)
                  for sale in sales if sale.lead_id in lead_created and start <= sale.occurred_at.date() <= end]
    lost_events = (await db.scalars(select(ClientLeadEvent).join(
        ClientLead, ClientLeadEvent.lead_id == ClientLead.id).where(
        ClientLead.project_id == project.id, ClientLeadEvent.event_type == "LEAD_LOST"))).all()
    lost_counts = defaultdict(int)
    for event in lost_events:
        if start <= event.created_at.date() <= end:
            label = (event.description or "Не определено").split(": ", 1)[0]
            lost_counts[label] += 1
    campaign_sales = defaultdict(list)
    for sale in sales:
        key = attribution.get(sale.lead_id)
        if key:
            campaign_sales[key].append(sale)

    by_campaign = defaultdict(list)
    for row in campaign_rows:
        by_campaign[(row.connection_id, row.external_campaign_id)].append(row)
    # An attributed campaign remains visible without spend only when it has a
    # lead, qualification or sale event in this period.
    keys = set(by_campaign)
    for key, leads in campaign_leads.items():
        if any(start <= lead.created_at.date() <= end or
               bool(lead.qualified_at and start <= lead.qualified_at.date() <= end)
               for lead in leads):
            keys.add(key)
    for key, items in campaign_sales.items():
        if any(start <= sale.occurred_at.date() <= end for sale in items):
            keys.add(key)
    campaigns = []
    for connection_id, external_id in sorted(keys):
        key = (connection_id, external_id)
        rows = by_campaign[key]
        leads = campaign_leads[key]
        sales_for_campaign = campaign_sales[key]
        lead_count = sum(start <= lead.created_at.date() <= end for lead in leads)
        qualified_count = sum(bool(lead.qualified_at and start <= lead.qualified_at.date() <= end) for lead in leads)
        period_sales = [sale for sale in sales_for_campaign if start <= sale.occurred_at.date() <= end]
        sale_count = len(period_sales)
        revenue = _sum(sale.amount for sale in period_sales)
        if sale_count == 0 and lead_count:
            revenue = 0
        spend = _sum(row.spend for row in rows)
        clicks = sum(int(row.clicks or 0) for row in rows) if rows else None
        impressions = sum(int(row.impressions or 0) for row in rows) if rows else None
        values = {"spend": spend, "leads": lead_count if lead_count else None,
                  "qualified": qualified_count if qualified_count else (0 if lead_count else None),
                  "sales": sale_count if sale_count else (0 if lead_count else None), "revenue": revenue}
        values.update(_derived(values, result["economics"]["margin"]))
        period_leads = [lead for lead in leads if start <= lead.created_at.date() <= end]
        values.update(quality_fields(sum(lead.quality == "target" for lead in period_leads),
                                     sum(lead.quality == "non_target" for lead in period_leads), spend))
        hypothesis_id = assignments.get(key)
        connection = connection_map[connection_id]
        campaigns.append({"id": f"{connection_id}:{external_id}", "external_campaign_id": external_id,
                          "name": max(rows, key=lambda row: row.date).campaign_name if rows else external_id,
                          "connection_id": connection_id, "connection_name": connection.name,
                          "platform": connection.platform, "hypothesis_id": hypothesis_id,
                          "hypothesis_name": hypothesis_map[hypothesis_id].name if hypothesis_id else None,
                          "status": None, "clicks": clicks, "impressions": impressions,
                          "cpc": _ratio(spend, clicks), **values})
    campaigns.sort(key=lambda row: (row["spend"] or 0, row["name"]), reverse=True)
    return {"current": current, "previous": previous,
            "sales_cycle_days": sum(cycle_days) / len(cycle_days) if cycle_days else None,
            "lost_reasons": [{"label": label, "count": amount} for label, amount in
                             sorted(lost_counts.items(), key=lambda item: (-item[1], item[0]))],
            "non_target_reasons": [{"label": label, "count": amount} for label, amount in sorted(
                defaultdict_count(lead.quality_reason or "Без причины" for lead, _ in lead_rows
                                  if lead.quality == "non_target" and start <= lead.created_at.date() <= end).items(),
                key=lambda item: (-item[1], item[0]))],
            "connections": [{"id": row.id, "name": row.name, "platform": row.platform,
                             "status": row.status, "last_synced_at": row.last_synced_at} for row in connections],
            "hypotheses": [{"id": row.id, "name": row.name, "status": row.status}
                            for row in hypotheses],
            "campaigns": campaigns}
