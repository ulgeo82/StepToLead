import asyncio
import unittest
from datetime import date

from app.models.marketing import AdCampaignMetricDaily, AdConnection, AdMetricDaily, Project
from app.services.analytics_details import analytics_details
from app.services.result_analytics import ReportExclude, result_facts


class VatTests(unittest.TestCase):
    def setUp(self):
        from test_result import ResultTests
        ResultTests.setUp(self)
        async def seed():
            async with self.sessions() as db:
                db.add(AdConnection(id=1, workspace_id=1, project_id=1, name="Реклама", platform="yandex",
                                    external_account_id="1", access_token_encrypted="unused"))
                db.add(AdMetricDaily(connection_id=1, date=date(2026, 10, 16), spend=1000))
                db.add(AdCampaignMetricDaily(connection_id=1, date=date(2026, 10, 16), spend=200,
                                            external_campaign_id="a", campaign_name="Кампания"))
                await db.commit()
        asyncio.run(seed())

    def tearDown(self):
        from test_result import ResultTests
        ResultTests.tearDown(self)

    def facts(self, vat="without", excluded=False):
        async def run():
            async with self.sessions() as db:
                connection = await db.get(AdConnection, 1)
                connection.spend_vat = vat
                await db.commit()
                project = await db.get(Project, 1)
                exclusion = ReportExclude(frozenset(), frozenset(), frozenset({(1, "a")}),
                                          frozenset()) if excluded else None
                facts = await result_facts(db, project, date(2026, 10, 16), date(2026, 10, 16),
                                           exclude=exclusion)
                details = await analytics_details(db, project, date(2026, 10, 16), date(2026, 10, 16),
                                                  facts, exclusion)
                self.assertEqual(float((await db.get(AdMetricDaily, 1)).spend), 1000)
                return facts, details
        return asyncio.run(run())

    def test_without_vat_is_1220(self):
        facts, details = self.facts()
        self.assertEqual(facts["current"]["totals"]["spend"], 1220)
        self.assertEqual(details["campaigns"][0]["spend"], 244)

    def test_with_vat_is_1000(self):
        facts, details = self.facts("with")
        self.assertEqual(facts["current"]["totals"]["spend"], 1000)
        self.assertEqual(details["campaigns"][0]["spend"], 200)

    def test_excluded_campaign_has_same_vat_basis(self):
        self.assertEqual(self.facts(excluded=True)[0]["current"]["totals"]["spend"], 976)
        self.assertEqual(self.facts("with", excluded=True)[0]["current"]["totals"]["spend"], 800)

    def test_vat_api_and_validation(self):
        response = self.client.put("/api/ads/connections/1/vat", headers=self.origin,
                                   json={"spend_vat": "with"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["spend_vat"], "with")
        self.assertEqual(self.client.get("/api/ads?project_id=1").json()["platforms"][0]
                         ["accounts"][0]["spend_vat"], "with")
        self.assertEqual(self.client.put("/api/ads/connections/1/vat", headers=self.origin,
                                         json={"spend_vat": "bad"}).status_code, 422)
        self.assertEqual(self.client.put("/api/ads/connections/1/vat", headers={"Origin": "https://evil.test"},
                                         json={"spend_vat": "without"}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.put("/api/ads/connections/1/vat", headers=self.origin,
                                         json={"spend_vat": "with"}).status_code, 401)
