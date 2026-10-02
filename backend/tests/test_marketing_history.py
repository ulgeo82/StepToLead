import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.api.routes.marketing import (ConnectionCreate, _meta_metrics, _vk_campaign_detail,
                                      _vk_campaigns, _vk_metrics, _yandex_metrics)


class MarketingHistoryTests(unittest.TestCase):
    def connection(self, platform: str):
        return SimpleNamespace(id=7, platform=platform, external_account_id="client-login", access_token_encrypted="secret")

    @patch("app.api.routes.marketing.decrypt_secret", return_value="access-token")
    @patch("app.api.routes.marketing._request")
    def test_meta_loads_maximum_period_and_all_pages(self, request, _decrypt):
        request.side_effect = [
            (200, json.dumps({"data": [{"date_start": "2024-01-01", "spend": "10", "impressions": "100", "clicks": "5", "actions": []}],
                              "paging": {"next": "https://graph.facebook.com/v22.0/next-page"}})),
            (200, json.dumps({"data": [{"date_start": "2024-01-02", "spend": "20", "impressions": "200", "clicks": "10", "actions": [{"action_type": "lead", "value": "2"}]}]})),
        ]
        rows = _meta_metrics(self.connection("meta"))
        query = parse_qs(urlparse(request.call_args_list[0].args[0]).query)
        self.assertEqual(query["date_preset"], ["maximum"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["leads"], 0)
        self.assertEqual(request.call_count, 2)

    @patch("app.api.routes.marketing.time.sleep")
    @patch("app.api.routes.marketing.decrypt_secret", return_value="access-token")
    @patch("app.api.routes.marketing._request_full")
    def test_yandex_requests_all_time_and_waits_for_report(self, request, _decrypt, sleep):
        request.side_effect = [
            (202, "", {"retryin": "1"}),
            (200, "Date\tImpressions\tClicks\tCost\tConversions\n2023-02-01\t100\t7\t42.5\t1\n", {}),
        ]
        rows = _yandex_metrics(self.connection("yandex"))
        payload = request.call_args_list[0].kwargs["payload"]["params"]
        self.assertEqual(payload["DateRangeType"], "ALL_TIME")
        self.assertEqual(payload["SelectionCriteria"], {})
        self.assertEqual(len(rows), 1)
        sleep.assert_called_once_with(1)

    @patch("app.api.routes.marketing._vk_json")
    def test_vk_campaign_statistics_do_not_count_platform_goals_as_leads(self, request):
        request.side_effect = [
            {"items": [{"id": 42, "name": "Учебная кампания"}]},
            {"items": [{"id": 42, "rows": [{"date": "2026-09-28", "base":
                {"spent": "12.50", "shows": 100, "clicks": 3, "goals": 2}}]}]},
        ]
        rows = _vk_metrics("test-token")
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["spend"]), "12.50")
        self.assertEqual(rows[0]["leads"], 0)
        self.assertEqual(rows[0]["campaigns"][0]["external_campaign_id"], "42")
        self.assertIn("/api/v2/statistics/ad_plans/day.json", request.call_args_list[1].args[0])

    def test_vk_requires_client_credentials_not_a_pasted_access_token(self):
        with self.assertRaises(ValueError):
            ConnectionCreate(workspace_id=1, platform="vk_ads", name="VK Ads",
                             access_token="not-an-api-secret")

    @patch("app.api.routes.marketing._vk_json")
    def test_vk_campaigns_include_campaign_without_delivery(self, request):
        request.return_value = {"items": [{"id": 42, "name": "Учебная кампания", "status": "active",
                                          "budget_limit_day": "1000"}]}
        campaigns = _vk_campaigns("token")
        self.assertEqual(campaigns[0]["id"], "42")
        self.assertEqual(campaigns[0]["budget_limit_day"], "1000")
        self.assertNotIn("spend", campaigns[0])

    @patch("app.api.routes.marketing._vk_json")
    def test_vk_campaign_detail_only_includes_groups_and_ads_of_selected_campaign(self, request):
        request.side_effect = [
            {"id": 42, "name": "Учебная кампания", "budget_limit_day": "1000"},
            {"items": [{"id": 3, "ad_plan_id": 42}, {"id": 4, "ad_plan_id": 99}]},
            {"id": 3, "ad_plan_id": 42, "targetings": {"geo": {"regions": [1]}}},
            {"items": [{"id": 8, "ad_group_id": 3}, {"id": 9, "ad_group_id": 4}]},
        ]
        detail = _vk_campaign_detail("token", 42)
        self.assertEqual(len(detail["groups"]), 1)
        self.assertEqual(detail["groups"][0]["banners"][0]["id"], 8)
        self.assertEqual(detail["groups"][0]["targetings"]["geo"]["regions"], [1])
        self.assertEqual(request.call_args_list[3].args[2]["_ad_group_id__in"], "3")


if __name__ == "__main__":
    unittest.main()
