"""Demo portal: believable data on every screen, no real API work for it."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.api.routes import portal
from app.core.access import COOKIE
from app.services import ad_sync, monitor
import test_stage2

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class DemoTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def test_demo_company(self):
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        started = time.time()
        made = self.client.post("/api/marketing/workspaces/demo", headers=ORIGIN)
        self.assertEqual(made.status_code, 201, made.text)
        demo = made.json()
        self.assertGreater(demo["deals"], 300)
        self.assertLess(time.time() - started, 60)
        self.assertIn("Демо · Кухни «Северная»", [w["name"] for w in self.client.get("/api/marketing/workspaces").json()])
        self.client.cookies.clear()
        with patch.object(portal, "rate_limit", AsyncMock()):
            login = self.client.post("/api/portal/auth/login", headers=ORIGIN, json={"username": demo["username"], "password": demo["password"]})
        self.assertEqual(login.status_code, 200, login.text)
        projects = self.client.get("/api/result/projects").json()
        pid = projects[0]["id"]
        end = datetime.now(timezone.utc).date(); start = end - timedelta(days=29)
        result = self.client.get(f"/api/result?project_id={pid}&start={start}&end={end}").json()
        self.assertTrue(result, result)
        ads = self.client.get(f"/api/ads?project_id={pid}&start={start}&end={end}").json()
        platforms = {p["id"]: p for p in ads["platforms"]}
        self.assertEqual(set(platforms), {"yandex", "vk_ads", "avito_items", "yandex_maps"})
        self.assertGreater(platforms["yandex"]["current"]["leads"], 20)
        self.assertGreater(ads["current"]["sales"] or 0, 0)
        self.assertIsNotNone(ads["current"]["romi"])
        board = self.client.get(f"/api/crm/projects/{pid}/board").json()
        self.assertGreater(sum(c["total"] for c in board["columns"]), 50)
        calls = self.client.get(f"/api/crm/projects/{pid}/calls?days=30").json()
        self.assertGreater(calls["total"], 5)
        self.assertGreater(len(self.client.get(f"/api/crm/projects/{pid}/conversations?status=open").json()["items"]), 0)
        # nothing real is synced, polled or alerted for the demo
        self.assertEqual([i for i in self.run_db(ad_sync.due) if i > 7], [])
        self.assertEqual([p for p in self.run_db(monitor.problems) if p.get("client", "") and "Демо" in p["client"]], [])


del Base

if __name__ == "__main__":
    unittest.main()
