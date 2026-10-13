"""Read-only audit of observed campaign data; zero loss is not an estimated saving.

Hourly raw contract: ``hourly: [{hour: 0..23, spend, leads or conversions}]``.
Hours use the advertising report's timezone; no allocation of daily spend to hours.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median


def value(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def number(item):
    if item is None or isinstance(item, bool):
        return None
    try:
        result = Decimal(str(item))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def day(item):
    if isinstance(item, datetime):
        return item.astimezone(timezone.utc).date() if item.tzinfo else item.date()
    if isinstance(item, date):
        return item
    try:
        return date.fromisoformat(str(item)[:10])
    except ValueError:
        return None


def audit(project, connections, campaign_rows, leads, economics, *, today=None) -> dict:
    """No I/O or input mutation. ``today`` allows deterministic date-boundary tests."""
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=29)
    project_id = value(project, "id")
    connected = {value(c, "id"): c for c in connections
                 if value(c, "project_id") == project_id}
    campaigns = defaultdict(list)
    for row in campaign_rows:
        d = day(value(row, "date"))
        cid = value(row, "connection_id")
        external = value(row, "external_campaign_id")
        if cid in connected and external is not None and d and start <= d <= today:
            campaigns[(cid, str(external))].append(row)

    counts = defaultdict(set)
    all_leads = set()
    for lead, attribution in leads:
        d = day(value(lead, "created_at"))
        if not d or not start <= d <= today:
            continue
        if value(lead, "project_id", project_id) != project_id:
            continue
        all_leads.add(value(lead, "id"))
        if attribution is not None:
            cid = value(attribution, "connection_id")
            external = value(attribution, "external_campaign_id")
            if external is not None:
                key = (cid, str(external))
                if key in campaigns:
                    counts[key].add(value(lead, "id"))

    totals = {}
    for key, rows in campaigns.items():
        spends = [number(value(r, "spend")) for r in rows]
        clicks = [number(value(r, "clicks")) for r in rows]
        totals[key] = (sum((s for s in spends if s is not None), Decimal(0)),
                       sum((c for c in clicks if c is not None), Decimal(0)),
                       all(s is not None for s in spends), all(c is not None for c in clicks))
    cpcs = [s / c for s, c, known_s, known_c in totals.values()
            if known_s and known_c and c > 0 and s > 0]
    median_cpc = median(cpcs) if len(cpcs) >= 2 else None
    limit = number(value(economics, "allowable_cpl", value(economics, "limit_cpl")))
    findings = []
    losses = {}

    def finding(code, title, detail, key, loss=Decimal(0)):
        cid, external = key
        rows = campaigns[key]
        campaign = {"connection_id": cid, "external_campaign_id": external,
                    "campaign_name": value(rows[-1], "campaign_name") or external,
                    "connection_name": value(connected[cid], "name") or "Рекламный кабинет"}
        findings.append({"code": code, "title": title, "detail": detail,
                         "loss_rub": float(loss.quantize(Decimal("0.01"))), "campaigns": [campaign]})
        # Night/weekend spend is a subset of whole-campaign spend, never add it twice.
        losses[key] = max(losses.get(key, Decimal(0)), loss)

    for key, rows in sorted(campaigns.items()):
        spend, clicks, known_spend, known_clicks = totals[key]
        crm_count = len(counts[key])
        raw = [value(r, "raw") if isinstance(value(r, "raw"), dict) else {} for r in rows]
        conversions = [number(r.get("conversions")) for r in raw]
        no_utm = any(r.get("has_utm") is False for r in raw)
        # An explicitly unknown goal count prevents asserting zero goals for the whole period.
        bad_goals = any("conversions" in r and n is None for r, n in zip(raw, conversions))
        goals = sum((c for c in conversions if c is not None), Decimal(0))
        if known_spend and spend >= 3000 and not crm_count and goals == 0 and not bad_goals and not no_utm:
            finding("spend_no_leads", "Расход без заявок и целей",
                    "За 30 дней нет связанных заявок CRM и зарегистрированных целей в переданных данных. "
                    "Проверьте полноту отслеживания перед отключением кампании.", key, spend)
        if no_utm:
            finding("no_utm", "В ссылках нет UTM-меток",
                    "Кабинет явно сообщает об отсутствии UTM. Связь заявки с кампанией может теряться; "
                    "денежная потеря по этим данным не доказана.", key)
        if known_spend and known_clicks and clicks > 0 and median_cpc and spend / clicks >= 2 * median_cpc:
            finding("high_cpc", "Клик дороже медианы вдвое или больше",
                    f"CPC — {spend / clicks:.2f} ₽, медиана кампаний проекта — {median_cpc:.2f} ₽. "
                    "Высокий CPC сам по себе не доказывает потерю денег.", key)
        if known_spend and crm_count > 0 and limit is not None and spend / crm_count > limit:
            finding("cpl_over_limit", "Цена заявки выше допустимой",
                    f"CPL по заявкам CRM — {spend / crm_count:.2f} ₽, допустимый — {limit:.2f} ₽. "
                    "Заявки получены, поэтому расход не считается потерей без результата.", key)

        window_spend = Decimal(0)
        window_results = Decimal(0)
        hourly_known = known_spend
        for row, data in zip(rows, raw):
            hours = data.get("hourly", data.get("hourly_data"))
            if not isinstance(hours, list) or not hours:
                hourly_known = False
                break
            daily_spend = Decimal(0)
            seen = set()
            for hour in hours:
                h = value(hour, "hour")
                cost = number(value(hour, "spend"))
                if type(h) is not int or not 0 <= h <= 23 or h in seen or cost is None:
                    hourly_known = False
                    break
                seen.add(h)
                daily_spend += cost
                if h < 7 or day(value(row, "date")).weekday() >= 5:
                    outcomes = number(value(hour, "leads"))
                    goals_in_hour = number(value(hour, "conversions"))
                    if outcomes is None and goals_in_hour is None:
                        hourly_known = False
                        break
                    window_results += max(outcomes or Decimal(0), goals_in_hour or Decimal(0))
                    window_spend += cost
            if not hourly_known or daily_spend != number(value(row, "spend")):
                hourly_known = False
                break
        if hourly_known and window_spend > 0 and window_results == 0 and spend > 0:
            finding("night_weekend", "Ночные и выходные расходы без результата",
                    f"В окнах 00:00–07:00 и в выходные потрачено {window_spend:.2f} ₽ "
                    f"({window_spend / spend * 100:.1f}% расхода). В почасовом отчёте нет заявок/целей "
                    "в этих окнах; пересекающиеся часы учтены один раз.", key, window_spend)

    return {"findings": findings,
            "total_loss_rub": float(sum(losses.values(), Decimal(0)).quantize(Decimal("0.01"))),
            "period_days": 30,
            "spend": float(sum((v[0] for v in totals.values()), Decimal(0)).quantize(Decimal("0.01"))),
            "leads": len(all_leads)}
