"""Observed-data boundaries for advertising audit; run with unittest discover."""
import asyncio
import copy
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from app.services.ad_audit import audit

TODAY = date(2026, 10, 13)
PROJECT = {"id": 1}
CONNECTIONS = [{"id": 1, "project_id": 1, "name": "Директ"},
               {"id": 2, "project_id": 1, "name": "VK"}]


def metric(campaign="a", spend=3000, clicks=30, raw=None, d=TODAY, connection=1):
    return {"connection_id": connection, "external_campaign_id": campaign, "campaign_name": campaign,
            "date": d, "spend": spend, "clicks": clicks, "raw": raw or {}}


def lead(i=1, campaign="a", connection=1, d=TODAY):
    return ({"id": i, "created_at": d, "project_id": 1},
            {"connection_id": connection, "external_campaign_id": campaign})


class AdAuditTests(unittest.TestCase):
    def run_audit(self, rows=(), leads=(), economics=None):
        return audit(PROJECT, CONNECTIONS, rows, leads, economics or {}, today=TODAY)

    def codes(self, result):
        return {f["code"] for f in result["findings"]}

    def test_empty(self):
        self.assertEqual(self.run_audit(), {"findings": [], "total_loss_rub": 0,
                                          "period_days": 30, "spend": 0, "leads": 0})

    def test_no_leads_threshold_and_results(self):
        self.assertIn("spend_no_leads", self.codes(self.run_audit([metric()])))
        self.assertNotIn("spend_no_leads", self.codes(self.run_audit([metric(spend=2999.99)])))
        self.assertNotIn("spend_no_leads", self.codes(self.run_audit([metric()], [lead()])))
        self.assertNotIn("spend_no_leads", self.codes(self.run_audit([metric(raw={"conversions": 1})])))
        self.assertIn("spend_no_leads", self.codes(self.run_audit([metric(raw={"conversions": 0})])))

    def test_unknown_conversions_and_missing_utm_do_not_prove_no_results(self):
        for raw in ({"conversions": None}, {"conversions": "broken"}, {"has_utm": False}):
            with self.subTest(raw=raw):
                self.assertNotIn("spend_no_leads", self.codes(self.run_audit([metric(raw=raw)])))

    def test_explicit_utm_only_and_zero_loss(self):
        for raw, expected in (({}, False), ({"has_utm": True}, False),
                              ({"has_utm": "false"}, False), ({"has_utm": False}, True)):
            result = self.run_audit([metric(raw=raw)])
            self.assertEqual("no_utm" in self.codes(result), expected)
            for finding in result["findings"]:
                if finding["code"] == "no_utm":
                    self.assertEqual(finding["loss_rub"], 0)

    def test_cpc_median_threshold_and_no_clicks(self):
        rows = [metric("a", 100, 10), metric("b", 100, 10), metric("c", 200, 10)]
        result = self.run_audit(rows)
        found = [f for f in result["findings"] if f["code"] == "high_cpc"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["campaigns"][0]["external_campaign_id"], "c")
        self.assertEqual(found[0]["loss_rub"], 0)
        rows[-1]["spend"] = 199.99
        self.assertNotIn("high_cpc", self.codes(self.run_audit(rows)))
        self.assertNotIn("high_cpc", self.codes(self.run_audit([metric(clicks=0)])))
        self.assertNotIn("high_cpc", self.codes(self.run_audit([metric()])))

    def test_cpl_strict_limit_and_missing_economics(self):
        rows = [metric(spend=2000)]
        self.assertIn("cpl_over_limit", self.codes(self.run_audit(rows, [lead()], {"allowable_cpl": 1999})))
        for economics in ({}, {"allowable_cac": 100}, {"allowable_cpl": None}, {"limit_cpl": 2000}):
            self.assertNotIn("cpl_over_limit", self.codes(self.run_audit(rows, [lead()], economics)))
        self.assertNotIn("cpl_over_limit", self.codes(self.run_audit(rows, [], {"limit_cpl": 100})))
        self.assertEqual(self.run_audit(rows, [lead()], {"limit_cpl": 100})["total_loss_rub"], 0)

    def test_hourly_night_and_seven_boundary(self):
        raw = {"hourly": [{"hour": 6, "spend": 100, "leads": 0},
                          {"hour": 7, "spend": 900, "leads": 2}]}
        result = self.run_audit([metric(spend=1000, raw=raw)])
        self.assertIn("night_weekend", self.codes(result))
        self.assertEqual(result["total_loss_rub"], 100)
        raw["hourly"][0]["leads"] = 1
        self.assertNotIn("night_weekend", self.codes(self.run_audit([metric(spend=1000, raw=raw)])))
        self.assertNotIn("night_weekend", self.codes(self.run_audit([metric(spend=1000)])))

    def test_weekend_union_and_loss_deduplication(self):
        saturday = date(2026, 10, 10)
        raw = {"conversions": 0, "hourly": [{"hour": 1, "spend": 1000, "conversions": 0},
                                           {"hour": 12, "spend": 2000, "conversions": 0}]}
        result = self.run_audit([metric(raw=raw, d=saturday)])
        self.assertEqual(self.codes(result), {"spend_no_leads", "night_weekend"})
        self.assertEqual(result["total_loss_rub"], 3000)
        self.assertEqual([f["loss_rub"] for f in result["findings"]], [3000, 3000])

    def test_hourly_unknown_or_inconsistent_skipped(self):
        for hours in ([{"hour": 0, "spend": 3000}], [{"hour": 0, "spend": 1000, "leads": 0}],
                      [{"hour": 24, "spend": 3000, "leads": 0}],
                      [{"hour": 1, "spend": 1500, "leads": 0}] * 2):
            with self.subTest(hours=hours):
                self.assertNotIn("night_weekend", self.codes(self.run_audit([metric(raw={"hourly": hours})])))

    def test_date_range_scope_and_attribution_connection(self):
        rows = [metric(d=TODAY - timedelta(days=29)), metric("old", d=TODAY - timedelta(days=30)),
                metric("future", d=TODAY + timedelta(days=1)), metric("foreign", connection=999)]
        result = self.run_audit(rows, [lead(connection=2), lead(2, d=TODAY - timedelta(days=30))])
        self.assertEqual(result["spend"], 3000)
        self.assertEqual(result["leads"], 1)
        self.assertIn("spend_no_leads", self.codes(result))
        # Same campaign id in two cabinets must not receive the same lead.
        result = self.run_audit([metric(), metric(connection=2)], [lead(connection=2)])
        self.assertEqual(result["total_loss_rub"], 3000)

    def test_duplicates_and_purity(self):
        rows, leads = [metric()], [lead(), lead()]
        before = copy.deepcopy((rows, leads))
        result = self.run_audit(rows, leads)
        self.assertEqual(result["leads"], 1)
        self.assertEqual((rows, leads), before)

    def test_unknown_numeric_data(self):
        result = self.run_audit([metric(spend=None, clicks=None, raw={"has_utm": False})])
        self.assertEqual(self.codes(result), {"no_utm"})
        self.assertEqual(result["total_loss_rub"], 0)


class AdAuditAPITests(unittest.TestCase):
    # Reuse the established authenticated FastAPI/database fixture, not its unrelated test methods.
    def setUp(self):
        from test_result import ResultTests
        ResultTests.setUp(self)

    def tearDown(self):
        from test_result import ResultTests
        ResultTests.tearDown(self)

    def test_api_cache_expiry_and_preserved_portal_state(self):
        from app.models.marketing import AdCampaignMetricDaily, AdConnection, Project
        async def seed():
            async with self.sessions() as db:
                (await db.get(Project, 1)).portal_state = {"ai": {"knowledge": "Не менять"}}
                db.add(AdConnection(id=1, workspace_id=1, project_id=1, platform="yandex", name="Кабинет",
                                    external_account_id="1", access_token_encrypted="unused"))
                db.add(AdCampaignMetricDaily(connection_id=1, date=datetime.now(timezone.utc).date(),
                                            external_campaign_id="a", campaign_name="Кампания", spend=3000))
                await db.commit()
        asyncio.run(seed())
        first = self.client.get("/api/result/audit?project_id=1")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["total_loss_rub"], 3000)
        with patch("app.services.ad_audit.audit", side_effect=AssertionError("Cache missed")):
            self.assertEqual(self.client.get("/api/result/audit?project_id=1").json(), first.json())
        async def expire():
            async with self.sessions() as db:
                project = await db.get(Project, 1)
                self.assertEqual(project.portal_state["ai"], {"knowledge": "Не менять"})
                project.portal_state = {**project.portal_state, "audit_cache": {
                    **project.portal_state["audit_cache"],
                    "created_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()}}
                await db.commit()
        asyncio.run(expire())
        with patch("app.services.ad_audit.audit", wraps=audit) as builder:
            self.assertEqual(self.client.get("/api/result/audit?project_id=1").status_code, 200)
            builder.assert_called_once()

    def test_auth_and_analytics_permission_even_on_cached_response(self):
        self.assertEqual(self.client.get("/api/result/audit?project_id=1").status_code, 200)
        with patch("app.api.routes.result.require_actor_permission",
                   side_effect=HTTPException(403, "Недостаточно прав")) as permission:
            self.assertEqual(self.client.get("/api/result/audit?project_id=1").status_code, 403)
            self.assertEqual(permission.call_args.args[2], "view_analytics")
        self.assertEqual(self.client.get("/api/result/audit?project_id=999").status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/result/audit?project_id=1").status_code, 401)
