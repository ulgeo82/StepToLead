"""«Не считать в аналитику» у лида — всегда; гипотеза, кампания и канал — исключения одного отчёта."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.db import Base
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesis, AdHypothesisCampaign,
                                  AdMetricDaily, ClientLead, ClientLeadAttribution, ClientSale, ClientWorkspace,
                                  Project)
from app.services import result_analytics


class AnalyticsExclusionTests(unittest.TestCase):
    def test_flagged_lead_and_hypothesis_are_excluded(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        day = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        async def go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(ClientWorkspace(id=2, name="Клиент"))
                db.add(Project(id=27, workspace_id=2, name="Проект", is_default=True))
                await db.flush()
                ad = AdConnection(workspace_id=2, project_id=27, platform="vk_ads", name="VK", external_account_id="1",
                                  access_token_encrypted="x", status="connected")
                db.add(ad); await db.flush()
                test_h = AdHypothesis(workspace_id=2, project_id=27, name="Тест")
                main_h = AdHypothesis(workspace_id=2, project_id=27, name="Основная")
                db.add_all([test_h, main_h]); await db.flush()
                db.add_all([AdHypothesisCampaign(hypothesis_id=test_h.id, connection_id=ad.id, external_campaign_id="c1"),
                            AdHypothesisCampaign(hypothesis_id=main_h.id, connection_id=ad.id, external_campaign_id="c2")])
                db.add(AdMetricDaily(connection_id=ad.id, date=day.date(), spend=1000, impressions=100, clicks=10))
                db.add_all([AdCampaignMetricDaily(connection_id=ad.id, date=day.date(), external_campaign_id="c1",
                                                  campaign_name="Тестовая", spend=400, impressions=40, clicks=4),
                            AdCampaignMetricDaily(connection_id=ad.id, date=day.date(), external_campaign_id="c2",
                                                  campaign_name="Основная", spend=600, impressions=60, clicks=6)])
                leads = {}
                for name, campaign, flag in [("Из теста", "c1", False), ("Из основной", "c2", False),
                                             ("Отмечен", None, True), ("Сайт", None, False)]:
                    lead = ClientLead(workspace_id=2, project_id=27, full_name=name, source="Сайт" if not campaign else "VK",
                                      created_at=day, exclude_from_analytics=flag)
                    db.add(lead); await db.flush(); leads[name] = lead
                    if campaign:
                        db.add(ClientLeadAttribution(lead_id=lead.id, connection_id=ad.id, external_campaign_id=campaign))
                db.add_all([ClientSale(lead_id=leads["Из теста"].id, project_id=27, amount=5000, occurred_at=day),
                            ClientSale(lead_id=leads["Отмечен"].id, project_id=27, amount=7000, occurred_at=day),
                            ClientSale(lead_id=leads["Сайт"].id, project_id=27, amount=3000, occurred_at=day)])
                await db.commit()
                project = await db.get(Project, 27)
                exclude = result_analytics.parse_report_exclude({"hypotheses": [test_h.id]})
                period = (date(2026, 10, 1), date(2026, 10, 11))
                plain = await result_analytics.result_facts(db, project, *period, "day")
                facts = await result_analytics.result_facts(db, project, *period, "day", exclude=exclude)
                from app.services.analytics_details import analytics_details
                details = await analytics_details(db, project, *period, facts, exclude)
                no_vk = await result_analytics.result_facts(
                    db, project, *period, "day", exclude=result_analytics.parse_report_exclude({"channels": [f"ad:{ad.id}"]}))
                from app.services.reports import build_report
                report = await build_report(db, project, *period, exclude)
                self.costed = await build_report(db, project, *period, exclude, {
                    "extra_costs": [{"name": "Гонорар", "amount": 300}], "metrics": ["drr", "leads", "revenue", "spend"]})
                site_id = leads["Сайт"].id
                self.no_deal = await build_report(db, project, *period,
                                                  result_analytics.parse_report_exclude({"hypotheses": [test_h.id], "leads": [site_id]}))
                self.plain, self.no_vk, self.report = plain, no_vk, report
                return facts, details
        try:
            facts, details = asyncio.run(go())
        finally:
            asyncio.run(engine.dispose())
            os.unlink(path)
        totals = facts["current"]["totals"]
        self.assertEqual(totals["leads"], 2)
        self.assertEqual(totals["sales"], 1)
        self.assertAlmostEqual(float(totals["spend"]), 600.0)
        self.assertAlmostEqual(float(totals["revenue"]), 3000.0)
        self.assertEqual(details["current"]["clicks"], 6)
        campaigns = {row["external_campaign_id"]: row for row in details["campaigns"] if row["kind"] == "ad"}
        self.assertNotIn("c1", campaigns)
        self.assertEqual(campaigns["c2"]["leads"], 1)
        # Без исключений отчёта: лид с отметкой в CRM всё равно не считается, гипотеза — считается.
        self.assertEqual(self.plain["current"]["totals"]["leads"], 3)
        self.assertAlmostEqual(float(self.plain["current"]["totals"]["spend"]), 1000.0)
        # Без канала VK остаётся только сайт.
        self.assertEqual(self.no_vk["current"]["totals"]["leads"], 1)
        self.assertIsNone(self.no_vk["current"]["totals"]["spend"])
        self.assertEqual(self.report["totals"]["leads"], 2)
        self.assertEqual(self.report["excluded"]["crm_leads"], 1)
        # Исключённая гипотеза видна карточкой «не в итогах» и перечислена в блоке прозрачности.
        cards = {h["name"]: h for h in self.report["hypotheses"]}
        self.assertTrue(cards["Основная"]["in_totals"])
        self.assertFalse(cards["Тест"]["in_totals"])
        self.assertEqual(cards["Тест"]["leads"], 1)
        self.assertEqual([h["name"] for h in self.report["excluded"]["hypotheses"]], ["Тест"])
        # Доп. расходы входят в расходы, ДРР считается от выручки.
        self.assertAlmostEqual(float(self.costed["totals"]["spend"]), 900.0)
        self.assertAlmostEqual(float(self.costed["totals"]["drr"]), 30.0)
        self.assertEqual(self.costed["hero"], ["drr", "leads", "revenue", "spend"])
        # Сделку можно снять с расчёта: продажа «Сайт» уходит, лид — тоже.
        self.assertEqual(self.no_deal["totals"]["sales"], 0)
        self.assertEqual([d["name"] for d in self.no_deal["excluded"]["deals"]], ["Сайт"])


if __name__ == "__main__":
    unittest.main()
