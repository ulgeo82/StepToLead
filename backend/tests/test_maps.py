"""Yandex Maps / 2GIS channels: monthly spend, statistics files, UTM attribution, ads overview."""
import io
import unittest
from datetime import date, datetime, timedelta, timezone

from openpyxl import Workbook
from sqlalchemy import func, select

import test_stage2
from app.models.crm import CrmDeal
from app.models.marketing import AdConnection, AdMetricDaily
from app.services import maps

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class MapsTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db, lead = Base.setUp, Base.tearDown, Base.as_user, Base.run_db, Base.lead

    def test_parse_yandex_business_export(self):
        wb = Workbook(); ws = wb.active
        ws.append(["Статистика компании «Ромакс»"]); ws.append([])
        ws.append(["Дата", "Переходов в профиль компании", "Проложено маршрутов", "Нажатий «Позвонить»", "Переходов на сайт"])
        ws.append([datetime(2026, 9, 1), 120, 14, 9, 21]); ws.append(["02.09.2026", "130", "10", "11", "18"])
        sheet = wb.create_sheet("Реклама"); sheet.append(["Период", "Просмотров рекламы", "Расход, ₽"])
        sheet.append(["1 сентября 2026", 2400, "1 000,50"])
        buf = io.BytesIO(); wb.save(buf)
        days, found = maps.parse_stats("stat.xlsx", buf.getvalue())
        self.assertEqual(found, ["calls", "clicks", "impressions", "routes", "site", "spend"])
        self.assertEqual(days[date(2026, 9, 1)], {"clicks": 120, "routes": 14, "calls": 9, "site": 21, "impressions": 2400, "spend": 1000.5})
        self.assertEqual(days[date(2026, 9, 2)]["calls"], 11)
        self.assertEqual(maps.spread(1000, 30, True)[:2], [33.34, 33.34])
        self.assertAlmostEqual(sum(maps.spread(1000, 30, True)), 1000)

    def test_map_channel_spend_stats_and_leads(self):
        created = self.client.post("/api/ads/maps", headers=ORIGIN, json={"project_id": 1, "platform": "yandex_maps", "name": "Яндекс Карты · Ромакс",
                                                                         "card_url": "https://yandex.ru/maps/org/romax/123"})
        self.assertEqual(created.status_code, 201, created.text)
        cid = created.json()["id"]
        self.assertEqual(self.client.post("/api/ads/maps", headers=ORIGIN, json={"project_id": 1, "platform": "yandex_maps", "name": "Дубль"}).status_code, 409)
        today = datetime.now(timezone.utc).date()
        month = today.strftime("%Y-%m")
        self.assertEqual(self.client.put(f"/api/ads/maps/{cid}/month", headers=ORIGIN, json={"month": month, "budget": 15000}).status_code, 200)
        spend = self.run_db(lambda db: db.scalar(select(func.sum(AdMetricDaily.spend)).where(AdMetricDaily.connection_id == cid)))
        self.assertAlmostEqual(float(spend), 15000, places=2)

        csv_body = "Дата;Показы;Нажатий «Позвонить»;Проложено маршрутов;Расход\n" + f"{today.strftime('%d.%m.%Y')};500;7;3;999\n"
        up = self.client.post(f"/api/ads/maps/{cid}/stats", headers=ORIGIN, files={"file": ("yb.csv", csv_body.encode("utf-8"), "text/csv")})
        self.assertEqual(up.status_code, 200, up.text)
        self.assertEqual(up.json()["days"], 1)
        # budget for the month wins over spend in the file
        spend = self.run_db(lambda db: db.scalar(select(func.sum(AdMetricDaily.spend)).where(AdMetricDaily.connection_id == cid)))
        self.assertAlmostEqual(float(spend), 15000, places=2)

        deal_id = self.lead("Ирина", "89275550011", "m1", utm_source="yandex_maps", utm_medium="maps")
        deal = self.run_db(lambda db: db.get(CrmDeal, deal_id))
        self.assertEqual(deal.attribution_snapshot.get("connection_id"), cid)
        self.assertEqual(deal.attribution_snapshot.get("platform"), "yandex_maps")

        detail = self.client.get(f"/api/ads/maps/{cid}?start={today.replace(day=1)}&end={today}").json()
        self.assertEqual((detail["totals"]["impressions"], detail["totals"]["calls"], detail["totals"]["routes"]), (500, 7, 3))
        self.assertIn("utm_source=yandex_maps", detail["utm_link"])
        self.assertEqual(detail["budgets"], [{"month": month, "amount": 15000}])

        overview = self.client.get(f"/api/ads?project_id=1&start={today.replace(day=1)}&end={today}").json()
        platform = next(p for p in overview["platforms"] if p["id"] == "yandex_maps")
        self.assertEqual(platform["name"], "Яндекс Карты")
        self.assertEqual(platform["current"]["leads"], 1)
        self.assertGreater(platform["current"]["spend"], 0)
        self.assertEqual(self.client.post(f"/api/ads/connections/{cid}/sync", headers=ORIGIN).status_code, 200)

        gis = self.client.post("/api/ads/maps", headers=ORIGIN, json={"project_id": 1, "platform": "2gis", "name": "2ГИС"}).json()
        r = self.client.put(f"/api/ads/maps/{gis['id']}/month", headers=ORIGIN, json={"month": month, "budget": 9000, "impressions": 3000, "calls": 30, "routes": 12})
        self.assertEqual(r.status_code, 200, r.text)
        start, end = maps.month_bounds(month)
        d = self.client.get(f"/api/ads/maps/{gis['id']}?start={start}&end={end}").json()
        self.assertEqual((d["totals"]["impressions"], d["totals"]["calls"], d["totals"]["routes"], d["totals"]["spend"]), (3000, 30, 12, 9000))
        self.as_user(2)
        self.assertEqual(self.client.put(f"/api/ads/maps/{gis['id']}/month", headers=ORIGIN, json={"month": month, "budget": 1}).status_code, 403)


del Base

if __name__ == "__main__":
    unittest.main()
