"""Токены ИИ: пакет по тарифу, перенос, резерв на обязательные отчёты, покупка, предупреждение 25%."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

from app.core.access import COOKIE
from app.models.ai import AiTokenAccount, AiTokenPurchase, AiUsage
from app.models.marketing import ClientWorkspace, PortalNotification
from app.services import ai, ai_tokens, ai_usage
import test_stage2


class AiTokensTests(unittest.TestCase):
    tearDown, as_user, run_db = test_stage2.Stage2Tests.tearDown, test_stage2.Stage2Tests.as_user, test_stage2.Stage2Tests.run_db

    def setUp(self):
        test_stage2.Stage2Tests.setUp(self)
        p = patch.object(ai_usage, "SessionLocal", self.sessions); p.start(); self.addCleanup(p.stop)
        self.set_plan("growth")

    def set_plan(self, plan):
        async def go(db):
            (await db.get(ClientWorkspace, 1)).plan = plan; await db.commit()
        self.run_db(go)

    def spend(self, tokens, bucket="client", when=None):
        async def go(db):
            db.add(AiUsage(workspace_id=1, feature="chat", provider="x", model="secret-model", bucket=bucket,
                           tokens_in=tokens, tokens_out=0, ok=True, created_at=when or datetime.now(timezone.utc)))
            await db.commit()
        self.run_db(go)

    def state(self):
        async def go(db):
            return await ai_tokens.status(db, 1)
        return self.run_db(go)

    def test_package_low_warning_and_empty_blocks_client_not_reserve(self):
        self.spend(1_600_000)
        s = self.state()
        self.assertEqual((s["package"], s["remaining"], s["level"]), (2_000_000, 400_000, "low"))
        async def settle(db):
            await ai_tokens.settle(db, 1); await db.commit()
            return (await db.scalars(select(PortalNotification.title).where(PortalNotification.user_id == 1))).all()
        self.assertEqual(self.run_db(settle), ["Осталось около 25% токенов ИИ"])
        self.assertEqual(self.run_db(settle), ["Осталось около 25% токенов ИИ"])  # один раз в месяц
        self.spend(400_000)
        async def admit(db, bucket):
            await ai_tokens.admit(db, 1, bucket, 1000)
        with self.assertRaises(ai.AIError):
            self.run_db(lambda db: admit(db, "client"))
        self.run_db(lambda db: admit(db, "reserve"))   # бриф/отчёты — из скрытого резерва
        self.run_db(lambda db: admit(db, "agency"))
        self.spend(300_000, bucket="reserve")
        with self.assertRaises(ai.AIError):
            self.run_db(lambda db: admit(db, "reserve"))

    def test_reserve_not_counted_until_exhausted_and_carryover(self):
        start = ai_tokens.month_start(datetime.now(timezone.utc))
        async def old(db):
            (await db.get(ClientWorkspace, 1)).created_at = start - timedelta(days=60); await db.commit()
        self.run_db(old)
        self.spend(500_000, when=start - timedelta(days=3))
        self.spend(200_000, bucket="reserve")
        s = self.state()
        self.assertEqual((s["carryover"], s["package"], s["used"]), (1_500_000, 3_500_000, 0))
        self.spend(150_000, bucket="reserve")  # 350k > резерва 300k → 50k из пакета
        self.assertEqual(self.state()["used"], 50_000)

    def test_bought_tokens_spent_after_package_and_persist(self):
        async def credit(db):
            row = AiTokenPurchase(workspace_id=1, tokens=1_000_000, price_rub=1490, provider="manual")
            db.add(row); await db.flush(); await ai_tokens.credit(db, row); await db.commit()
        self.run_db(credit)
        self.spend(2_500_000)
        async def settle(db):
            await ai_tokens.settle(db, 1); await db.commit()
            return await db.get(AiTokenAccount, 1)
        account = self.run_db(settle)
        self.assertEqual((account.bought_total, account.bought_spent), (1_000_000, 500_000))
        s = self.state()
        self.assertEqual((s["remaining"], s["bought_left"]), (500_000, 500_000))
        self.run_db(settle)
        self.assertEqual(self.run_db(settle).bought_spent, 500_000)  # повторный расчёт не списывает дважды

    def test_individual_unlimited_and_forecast(self):
        self.set_plan(None)
        self.assertTrue(self.state()["unlimited"])
        self.set_plan("growth")
        self.spend(700_000, when=datetime.now(timezone.utc) - timedelta(days=6, hours=23))
        s = self.state()
        self.assertEqual(s["days_left"], 12)  # 1.3M / (700k / ~6.96 дн.)

    def test_api_hides_model_and_reserve_purchase_and_admin_confirm(self):
        self.spend(100_000)
        data = self.client.get("/api/ai-tokens").json()
        self.assertNotIn("reserve_left", data); self.assertNotIn("secret-model", str(data))
        self.assertTrue(data["can_buy"]); self.assertEqual(len(data["packs"]), 3)
        self.as_user(2)
        self.assertEqual(self.client.post("/api/ai-tokens/purchase", headers=test_stage2.ORIGIN, json={"pack": 0}).status_code, 403)
        self.as_user(1)
        with patch.dict("os.environ", {}, clear=False):
            bought = self.client.post("/api/ai-tokens/purchase", headers=test_stage2.ORIGIN, json={"pack": 0})
        self.assertEqual(bought.status_code, 200, bought.text)
        self.assertIsNone(bought.json()["payment_url"]); pid = bought.json()["purchase"]["id"]
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        admin = self.client.get("/api/portal/admin/ai-tokens").json()
        self.assertEqual([p["id"] for p in admin["pending"]], [pid])
        self.assertEqual(self.client.post(f"/api/portal/admin/ai-tokens/purchases/{pid}/paid", headers=test_stage2.ORIGIN).status_code, 200)
        self.assertEqual(self.client.post(f"/api/portal/admin/ai-tokens/purchases/{pid}/paid", headers=test_stage2.ORIGIN).status_code, 409)
        ws = self.client.get("/api/portal/admin/ai-tokens/workspaces/1").json()
        self.assertEqual(ws["bought_left"], 1_000_000); self.assertIn("reserve_left", ws)
        put = self.client.put("/api/portal/admin/ai-tokens/workspaces/1", headers=test_stage2.ORIGIN,
                              json={"monthly_override": 100_000, "reserve_override": None})
        self.assertEqual(put.json()["package"], 100_000 + 0)
        cfg = self.client.put("/api/portal/admin/ai-tokens/config", headers=test_stage2.ORIGIN,
                              json={"monthly": {"growth": 3_000_000}, "packs": [{"tokens": 500_000, "price": 990}]})
        self.assertEqual(cfg.json()["config"]["monthly"]["growth"], 3_000_000)
        self.assertEqual(cfg.json()["config"]["packs"], [{"tokens": 500_000, "price": 990}])


if __name__ == "__main__":
    unittest.main()
