"""Этап B (docs/OUTREACH_INBOX.md): Telegram-аутрич в пространстве клиента. Клиент A не видит и не трогает
аккаунты, прокси, кампании, очередь и задачи сбора клиента B; старые записи (без клиента) — агентские."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_stage2
from sqlalchemy import select

from app.core.access import COOKIE
from app.core.config import settings
from app.models import Campaign, Lead, Proxy, TelegramAccount
from app.models.marketing import ClientWorkspace
from app.services import telegram_jobs

ORIGIN = test_stage2.ORIGIN
HOME, CLIENT = 1, 2
AS_CLIENT = {**ORIGIN, "X-Workspace-Id": str(CLIENT)}


class TelegramSpaceTests(unittest.TestCase):
    setUp_base = test_stage2.Stage2Tests.setUp
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        self.setUp_base()
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, "a" * 43)
        self._old = (settings.agency_workspace_id, telegram_jobs.session_factory, telegram_jobs.spawn,
                     settings.encryption_secret)
        settings.agency_workspace_id = HOME
        settings.encryption_secret = "test-secret-for-sessions"
        telegram_jobs.session_factory = self.sessions
        self.spawned = []
        telegram_jobs.spawn = lambda coro: self.spawned.append(coro)

        async def seed(db):
            db.add(ClientWorkspace(id=CLIENT, name="OneSpot"))
            now = [[int(time.time()), 1]]
            db.add(Proxy(id=11, host="10.1.1.1", port=1080, pool="agency", status="working", max_accounts=1,
                         workspace_id=None, health={"checks": now}))          # старый, до переноса = агентство
            db.add(Proxy(id=12, host="10.2.2.2", port=1080, pool="agency", status="working", max_accounts=1,
                         workspace_id=CLIENT, health={"checks": now}))
            db.add(Proxy(id=13, host="10.3.3.3", port=1080, pool="client", status="working", max_accounts=3))
            db.add(TelegramAccount(id=21, phone="+79000000021", session_encrypted="x", proxy_id=11, status="online"))
            db.add(TelegramAccount(id=22, phone="+79000000022", session_encrypted="x", proxy_id=12, status="online",
                                   workspace_id=CLIENT))
            db.add(Campaign(id=31, name="Агентство"))
            db.add(Campaign(id=32, name="OneSpot", workspace_id=CLIENT))
            await db.flush()
            db.add(Lead(campaign_id=31, username="home_lead", first_message="Привет"))
            db.add(Lead(campaign_id=32, username="client_lead", first_message="Привет"))
            await db.commit()
        self.run_db(seed)

    def tearDown(self):
        (settings.agency_workspace_id, telegram_jobs.session_factory, telegram_jobs.spawn,
         settings.encryption_secret) = self._old
        test_stage2.Stage2Tests.tearDown(self)

    def ids(self, path, headers=None):
        r = self.client.get(path, headers=headers or {})
        self.assertEqual(r.status_code, 200, r.text)
        return sorted(x["id"] for x in r.json())

    def test_accounts_are_per_client(self):
        self.assertEqual(self.ids("/api/telegram-accounts"), [21])
        self.assertEqual(self.ids("/api/telegram-accounts", AS_CLIENT), [22])
        self.assertEqual(self.client.delete("/api/telegram-accounts/21", headers=AS_CLIENT).status_code, 404)
        self.assertEqual(self.client.post("/api/telegram-accounts/21/replace-proxy", headers=AS_CLIENT,
                                          json={"confirm_ip_change": True}).status_code, 404)
        summary = self.client.get("/api/telegram-accounts/health-summary", headers=AS_CLIENT).json()
        self.assertEqual((summary["accounts"]["total"], summary["proxies"]["total"]), (1, 1))

    def test_proxies_are_per_client_and_crm_pool_stays_with_agency(self):
        self.assertEqual(self.ids("/api/proxies"), [11, 13])
        self.assertEqual(self.ids("/api/proxies", AS_CLIENT), [12])
        self.assertEqual(self.client.delete("/api/proxies/11", headers=AS_CLIENT).status_code, 404)
        r = self.client.post("/api/proxies/bulk", headers=AS_CLIENT, json={"entries": "5.5.5.5:1080:u:p"})
        self.assertEqual(r.status_code, 201, r.text)
        added = self.run_db(lambda db: db.scalar(select(Proxy).where(Proxy.host == "5.5.5.5")))
        self.assertEqual(added.workspace_id, CLIENT)
        self.assertEqual(self.client.post("/api/proxies/bulk", headers=AS_CLIENT,
                                          json={"entries": "6.6.6.6:1080", "pool": "client"}).status_code, 422)

    def test_import_uses_only_own_proxies(self):
        # у клиента единственный прокси (12) уже занят аккаунтом 22, а прокси агентства (11) ему не положен
        files = [("files", ("a.zip", b"PK", "application/zip"))]
        r = self.client.post("/api/telegram-accounts/import-batch", headers=AS_CLIENT, files=files, data={"mode": "auto"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["without_proxy"], 1)      # прокси агентства (11) клиенту не достаётся
        bad = self.client.post("/api/telegram-accounts/import-batch", headers=AS_CLIENT, files=files,
                               data={"mode": "order", "proxy_ids": "11"})
        self.assertEqual(bad.status_code, 422)

    def test_campaigns_queue_and_dialogs(self):
        self.assertEqual(self.ids("/api/campaigns"), [31])
        self.assertEqual(self.ids("/api/campaigns", AS_CLIENT), [32])
        self.assertEqual(self.client.get("/api/campaigns/31", headers=AS_CLIENT).status_code, 404)
        self.assertEqual(self.client.get("/api/campaigns/31/dialogs", headers=AS_CLIENT).status_code, 404)
        self.assertEqual([l["username"] for l in self.client.get("/api/leads", headers=AS_CLIENT).json()],
                         ["client_lead"])
        created = self.client.post("/api/campaigns", headers=AS_CLIENT, json={"name": "Новая"}).json()
        self.assertEqual(self.run_db(lambda db: db.get(Campaign, created["id"])).workspace_id, CLIENT)
        wrong = self.client.put("/api/campaigns/32/automation", headers=AS_CLIENT,
                                json={"settings": {}, "account_ids": [21]})
        self.assertEqual(wrong.status_code, 422)          # аккаунт агентства в кампанию клиента — нельзя

    def test_parser_and_switched_off_client(self):
        r = self.client.post("/api/telegram-parser/tasks", headers=AS_CLIENT,
                             json={"account_id": 21, "sources": ["https://t.me/chat"]})
        self.assertEqual(r.status_code, 422)
        from app.models.automation import CampaignRuntime
        from app.services import campaign_runner

        async def off(db):
            (await db.get(ClientWorkspace, CLIENT)).status = "disabled"
            db.add(CampaignRuntime(campaign_id=32, status="running", settings={"sleep_periods": ""}))
            await db.commit()
        self.run_db(off)
        connect = AsyncMock()
        with patch.object(campaign_runner, "SessionLocal", self.sessions), \
                patch.object(campaign_runner, "connect_account", connect):
            asyncio.run(campaign_runner.tick(32))
        connect.assert_not_awaited()
        self.assertEqual(self.client.get("/api/telegram-accounts", headers=AS_CLIENT).status_code, 404)

    def test_move_accounts_with_their_proxy_to_client(self):
        r = self.client.post("/api/telegram-accounts/move", headers=ORIGIN, json={"ids": [21], "workspace_id": CLIENT})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["accounts"], r.json()["proxies"]), (1, 1))
        self.assertEqual(self.ids("/api/telegram-accounts"), [])
        self.assertEqual(self.ids("/api/telegram-accounts", AS_CLIENT), [21, 22])
        self.assertIn(11, self.ids("/api/proxies", AS_CLIENT))
        # чужой аккаунт не перенести; несуществующий клиент — 404
        self.assertEqual(self.client.post("/api/telegram-accounts/move", headers=ORIGIN,
                                          json={"ids": [22], "workspace_id": HOME}).status_code, 404)
        self.assertEqual(self.client.post("/api/telegram-accounts/move", headers=AS_CLIENT,
                                          json={"ids": [22], "workspace_id": 99}).status_code, 404)

    def test_move_refuses_account_in_other_clients_campaign_and_busy_proxy(self):
        from app.models.automation import CampaignAccount

        async def bind(db):
            db.add(CampaignAccount(campaign_id=31, account_id=21))
            await db.commit()
        self.run_db(bind)
        r = self.client.post("/api/telegram-accounts/move", headers=ORIGIN, json={"ids": [21], "workspace_id": CLIENT})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Агентство", r.json()["detail"])
        r = self.client.post("/api/proxies/move", headers=ORIGIN, json={"ids": [11], "workspace_id": CLIENT})
        self.assertEqual(r.status_code, 409)
        async def free(db):
            db.add(Proxy(id=14, host="10.4.4.4", port=1080, pool="agency", status="working", max_accounts=1))
            await db.commit()
        self.run_db(free)
        r = self.client.post("/api/proxies/move", headers=ORIGIN, json={"ids": [14], "workspace_id": CLIENT})
        self.assertEqual((r.status_code, r.json()["proxies"]), (200, 1))
        self.assertIn(14, self.ids("/api/proxies", AS_CLIENT))

    def test_avatar_thumb(self):
        import io
        from PIL import Image
        from app.services.avatar_variants import thumb
        buf = io.BytesIO(); Image.new("RGB", (400, 300), "red").save(buf, "PNG")
        data = thumb(buf.getvalue())
        self.assertTrue(data.startswith("data:image/jpeg;base64,"))
        self.assertLess(len(data), 8000)
        self.assertIsNone(thumb(b"not an image"))

    def test_first_touch_limit_company_and_account(self):
        from app.services import tg_limits
        self.assertEqual(self.client.get("/api/telegram-accounts/limits", headers=AS_CLIENT).json()["first_per_day"],
                         tg_limits.DEFAULT_FIRST_PER_DAY)
        r = self.client.put("/api/telegram-accounts/limits", headers=AS_CLIENT, json={"first_per_day": 25})
        self.assertEqual(r.json()["first_per_day"], 25)
        self.assertEqual(self.client.get("/api/telegram-accounts/limits").json()["first_per_day"],
                         tg_limits.DEFAULT_FIRST_PER_DAY)          # у агентства свой лимит
        self.assertEqual(self.client.patch("/api/telegram-accounts/22/limit", headers=AS_CLIENT,
                                           json={"daily_first_limit": 40}).status_code, 200)
        self.assertEqual(self.client.patch("/api/telegram-accounts/22/limit", headers=ORIGIN,
                                           json={"daily_first_limit": 40}).status_code, 404)

        async def limits(db):
            a22, a21 = await db.get(TelegramAccount, 22), await db.get(TelegramAccount, 21)
            return await tg_limits.first_limit(db, a22), await tg_limits.first_limit(db, a21)
        self.assertEqual(self.run_db(limits), (40, tg_limits.DEFAULT_FIRST_PER_DAY))
        self.client.patch("/api/telegram-accounts/22/limit", headers=AS_CLIENT, json={"daily_first_limit": None})
        self.assertEqual(self.run_db(limits)[0], 25)


if __name__ == "__main__":
    unittest.main()
