"""Прогрев Telegram (docs/TELEGRAM_WARMUP.md): этапы, ночь, рекомендация лимита, сессии, переписка своих,
без прокси — ни одного подключения, управление по клиентам."""
import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import test_stage2
from sqlalchemy import select

from app.core.access import COOKIE
from app.core.config import settings
from app.models import Proxy, TelegramAccount
from app.models.marketing import ClientWorkspace
from app.models.telegram_warmup import TelegramWarmupChat, TelegramWarmupEvent
from app.services import account_locks, telegram_jobs, tg_warmup, tg_warmup_bank

ORIGIN = test_stage2.ORIGIN
HOME, CLIENT = 1, 2
AS_CLIENT = {**ORIGIN, "X-Workspace-Id": str(CLIENT)}
NOON = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)   # 12:00 по Москве


class FakeClient:
    def __init__(self, log):
        self.log = log

    async def __call__(self, request):
        name = type(request).__name__
        self.log.append(name)
        if name == "GetPasswordRequest":
            return SimpleNamespace(has_password=False)
        if name == "ImportContactsRequest":
            phone = request.contacts[0].phone
            return SimpleNamespace(users=[SimpleNamespace(id=int(phone[-3:]), phone=phone)])
        return SimpleNamespace()

    async def edit_2fa(self, new_password, hint=""):
        self.log.append("edit_2fa")

    async def get_entity(self, name):
        return SimpleNamespace(username=name)

    async def get_messages(self, peer, limit=1):
        return [SimpleNamespace(id=i + 1, out=False) for i in range(min(limit, 3))]

    async def send_read_acknowledge(self, peer, max_id=None):
        self.log.append("read")

    async def send_message(self, peer, text):
        self.log.append(("send", peer.id, text))


class WarmupLogicTests(unittest.TestCase):
    def test_stages_and_recommended_limit(self):
        st = {"enabled": True, "started_at": NOON.isoformat()}
        self.assertEqual(tg_warmup.stage_of(st, NOON), "prep")
        st["prepared_at"] = NOON.isoformat()
        self.assertEqual([tg_warmup.stage_of(st, NOON + timedelta(days=d)) for d in (0, 3, 7, 20)],
                         ["rest", "passive", "social", "ready"])
        self.assertEqual([tg_warmup.recommended(st, NOON + timedelta(days=d), 15) for d in (0, 5, 9, 12, 30)],
                         [0, 2, 5, 8, 15])
        self.assertEqual(tg_warmup.recommended({**st, "warmed": True}, NOON, 15), 15)
        self.assertIsNone(tg_warmup.stage_of({"enabled": False}, NOON))

    def test_never_at_night(self):
        rnd = random.Random(1)
        late = datetime(2026, 11, 18, 20, 30, tzinfo=timezone.utc)    # 23:30 по Москве
        for account_id in range(1, 20):
            for _ in range(20):
                when = tg_warmup.next_session(late, "social", "normal", "Europe/Moscow", account_id, rnd)
                hour = (when + timedelta(hours=3)).hour
                self.assertTrue(7 <= hour < 24, (account_id, when))
                self.assertGreater(when, late)

    def test_bank_has_no_links_and_spins(self):
        rnd = random.Random(3)
        for topic in tg_warmup_bank.TOPICS:
            for turn in range(tg_warmup_bank.turns(topic)):
                text = tg_warmup_bank.line(topic, turn, rnd)
                self.assertNotIn("{", text)
                self.assertNotIn("http", text)
                self.assertLess(len(text), 200)


class WarmupSessionTests(unittest.TestCase):
    setUp_base = test_stage2.Stage2Tests.setUp
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        self.setUp_base()
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, "a" * 43)
        self._old = (settings.agency_workspace_id, tg_warmup.session_factory, settings.encryption_secret)
        settings.agency_workspace_id = HOME
        settings.encryption_secret = "test-secret-for-sessions"
        tg_warmup.session_factory = self.sessions
        self.calls = []
        self.connected = []

        async def fake_with(account, fn):
            self.connected.append(account.id)
            return await fn(FakeClient(self.calls))
        p = patch.object(telegram_jobs, "_with_account_unlocked", fake_with)
        p.start(); self.addCleanup(p.stop)
        s = patch("asyncio.sleep", self._nosleep)
        s.start(); self.addCleanup(s.stop)

        async def spam(client):
            return {"state": "none", "until": None}
        h = patch("app.services.account_health.ask_spambot", spam)
        h.start(); self.addCleanup(h.stop)

        async def seed(db):
            db.add(ClientWorkspace(id=CLIENT, name="OneSpot"))
            for pid in (11, 12, 13):
                db.add(Proxy(id=pid, host=f"10.0.0.{pid}", port=1080, pool="agency", status="working",
                             workspace_id=CLIENT if pid != 13 else None))
            db.add(TelegramAccount(id=21, phone="+79000000121", session_encrypted="x", proxy_id=11, workspace_id=CLIENT,
                                   first_name="Аня", telegram_user_id=121))
            db.add(TelegramAccount(id=22, phone="+79000000122", session_encrypted="x", proxy_id=12, workspace_id=CLIENT,
                                   first_name="Боря", telegram_user_id=122))
            db.add(TelegramAccount(id=23, phone="+79000000123", session_encrypted="x", proxy_id=None, workspace_id=CLIENT))
            db.add(TelegramAccount(id=24, phone="+79000000124", session_encrypted="x", proxy_id=13))
            await db.commit()
        self.run_db(seed)

    async def _nosleep(self, *_a, **_k):
        return None

    def tearDown(self):
        settings.agency_workspace_id, tg_warmup.session_factory, settings.encryption_secret = self._old
        test_stage2.Stage2Tests.tearDown(self)

    def session(self, account_id, now, seed=1):
        import asyncio
        asyncio.run(tg_warmup.run_session(account_id, random.Random(seed), now))

    def state(self, account_id):
        async def go(db):
            return (await db.get(TelegramAccount, account_id)).warmup or {}
        return self.run_db(go)

    def test_api_is_per_client_and_start_prepares_account(self):
        self.assertEqual(self.client.post("/api/telegram-warmup/start", headers=ORIGIN, json={"ids": [21]}).status_code, 404)
        r = self.client.post("/api/telegram-warmup/start", headers=AS_CLIENT, json={"ids": [21, 22, 23]})
        self.assertEqual(r.json()["done"], 3)
        rows = {a["id"]: a for a in self.client.get("/api/telegram-warmup/accounts", headers=AS_CLIENT).json()}
        self.assertEqual(set(rows), {21, 22, 23})
        self.assertEqual(rows[21]["stage"], "prep")
        self.session(21, NOON)
        self.assertIn("SetPrivacyRequest", self.calls)
        self.assertIn("edit_2fa", self.calls)
        self.assertIn("ResetAuthorizationsRequest", self.calls)
        st = self.state(21)
        self.assertTrue(st["prepared_at"])
        self.assertGreater(datetime.fromisoformat(st["next_at"]), NOON)
        pw = self.client.get("/api/telegram-warmup/accounts/21/twofa", headers=AS_CLIENT).json()["password"]
        self.assertGreaterEqual(len(pw), 12)
        self.assertEqual(self.client.get("/api/telegram-warmup/accounts/21/twofa").status_code, 404)

    def test_no_proxy_never_connects(self):
        self.client.post("/api/telegram-warmup/start", headers=AS_CLIENT, json={"ids": [23]})
        self.session(23, NOON)
        self.assertEqual(self.connected, [])
        self.assertIn("прокси", self.state(23)["last"])

    def test_busy_account_is_skipped(self):
        self.client.post("/api/telegram-warmup/start", headers=AS_CLIENT, json={"ids": [21]})
        account_locks.try_take(21)
        try:
            self.session(21, NOON)
        finally:
            account_locks.release(21)
        self.assertEqual(self.connected, [])

    def test_social_stage_chats_with_own_pool_and_partner_replies(self):
        self.client.post("/api/telegram-warmup/start", headers=AS_CLIENT, json={"ids": [21, 22]})
        start = NOON - timedelta(days=7)

        async def age(db):
            for aid in (21, 22):
                a = await db.get(TelegramAccount, aid)
                a.warmup = {**a.warmup, "started_at": start.isoformat(), "prepared_at": start.isoformat(),
                            "prep": {"privacy": "x", "twofa": "x", "sessions": "x"}}
            await db.commit()
        self.run_db(age)
        for seed in range(1, 30):        # с вероятностью 0.6 начинает диалог — найдём seed, где начал
            self.calls.clear()
            self.session(21, NOON, seed)
            chats = self.run_db(lambda db: db.scalars(select(TelegramWarmupChat)))
            chats = list(chats.all()) if hasattr(chats, "all") else list(chats)
            if chats:
                break
        self.assertTrue(chats, "диалог так и не начат")
        chat = chats[0]
        self.assertEqual((chat.a_id, chat.b_id, chat.next_from_id), (21, 22, 22))
        self.assertTrue(any(c[0] == "send" and c[1] == 122 for c in self.calls if isinstance(c, tuple)))
        # собеседник отвечает в свою сессию, когда подошло время
        import asyncio

        async def due(db):
            return await tg_warmup.due_accounts(db, chat.next_at + timedelta(seconds=1))
        self.assertIn(22, self.run_db(due))
        self.calls.clear()
        self.session(22, chat.next_at + timedelta(seconds=1))
        self.assertTrue(any(c[0] == "send" and c[1] == 121 for c in self.calls if isinstance(c, tuple)))
        events = self.client.get("/api/telegram-warmup/events", headers=AS_CLIENT).json()
        self.assertTrue(any("написал" in e["text"] for e in events))

    def test_pause_and_settings(self):
        self.client.post("/api/telegram-warmup/start", headers=AS_CLIENT, json={"ids": [21]})
        self.client.post("/api/telegram-warmup/pause", headers=AS_CLIENT, json={"ids": [21]})
        self.session(21, NOON)
        self.assertEqual(self.connected, [])
        r = self.client.put("/api/telegram-warmup/settings", headers=AS_CLIENT,
                            json={"intensity": "soft", "channels": ["https://t.me/durov", "@telegram"]})
        self.assertEqual(r.json()["channels"], ["durov", "telegram"])
        self.assertEqual(self.client.get("/api/telegram-warmup/settings").json()["intensity"], "normal")


if __name__ == "__main__":
    unittest.main()
