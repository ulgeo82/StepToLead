import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.api.routes.marketing import _meta_metrics, _yandex_metrics


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


if __name__ == "__main__":
    unittest.main()
