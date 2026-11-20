"""Рассылка компании (docs/TELEGRAM_COMPANY.md): гипотезы — базы получателей, одна активная, аккаунты и
настройки компании общие, дубли между гипотезами не импортируются."""
import unittest

import test_stage2
from sqlalchemy import select

from app.core.access import COOKIE
from app.core.config import settings
from app.models import Campaign, Lead, Proxy, TelegramAccount
from app.models.automation import CampaignAccount, CampaignDialog, CampaignRuntime
from app.models.marketing import ClientWorkspace

ORIGIN = test_stage2.ORIGIN
HOME, CLIENT = 1, 2
AS_CLIENT = {**ORIGIN, "X-Workspace-Id": str(CLIENT)}
B = "/api/telegram-company"


class CompanyTests(unittest.TestCase):
    setUp_base = test_stage2.Stage2Tests.setUp
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        self.setUp_base()
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        self._old = settings.agency_workspace_id
        settings.agency_workspace_id = HOME

        async def seed(db):
            db.add(ClientWorkspace(id=CLIENT, name="OneSpot"))
            for i in (1, 2):
                db.add(Proxy(id=10 + i, host=f"10.0.0.{i}", port=1080, pool="agency", status="working", workspace_id=CLIENT))
            await db.flush()
            db.add(TelegramAccount(id=21, phone="+79000000021", session_encrypted="x", proxy_id=11, workspace_id=CLIENT, status="online"))
            db.add(TelegramAccount(id=22, phone="+79000000022", session_encrypted="x", proxy_id=12, workspace_id=CLIENT, status="online"))
            db.add(TelegramAccount(id=23, phone="+79000000023", session_encrypted="x", proxy_id=None, workspace_id=CLIENT))
            db.add(TelegramAccount(id=24, phone="+79000000024", session_encrypted="x", proxy_id=11, workspace_id=CLIENT, status="banned"))
            await db.commit()
        self.run_db(seed)

    def tearDown(self):
        settings.agency_workspace_id = self._old
        test_stage2.Stage2Tests.tearDown(self)

    def hyp(self, name):
        r = self.client.post(f"{B}/hypotheses", headers=AS_CLIENT, json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def upload(self, cid, rows):
        text = "username,first_message\n" + "".join(f"{u},Привет {u}\n" for u in rows)
        return self.client.post(f"/api/campaigns/{cid}/import", headers=AS_CLIENT,
                                files={"file": ("base.csv", text.encode(), "text/csv")}).json()

    def test_hypotheses_share_company_accounts_and_skip_duplicates(self):
        a, b = self.hyp("Агентства Авито"), self.hyp("Фрилансеры")
        self.assertEqual(self.upload(a, ["one", "two"])["imported"], 2)
        r = self.upload(b, ["two", "three"])
        self.assertEqual((r["imported"], r["already_in_company"]), (1, 1))

        async def bindings(db):
            return sorted((c, x) for c, x in (await db.execute(select(CampaignAccount.campaign_id, CampaignAccount.account_id))).all())
        self.assertEqual(self.run_db(bindings), [(a, 21), (a, 22), (b, 21), (b, 22)])   # без прокси и забаненный — нет
        data = self.client.get(B, headers=AS_CLIENT).json()
        self.assertEqual([h["total"] for h in data["hypotheses"]], [2, 1])
        self.assertEqual(data["accounts"], 2)
        self.assertEqual(self.client.get(B).json()["hypotheses"], [])   # у агентства своих гипотез нет

    def test_one_active_hypothesis_and_company_start(self):
        a, b = self.hyp("A"), self.hyp("B")
        self.upload(a, ["one"]); self.upload(b, ["two"])
        r = self.client.put(f"{B}/settings", headers=AS_CLIENT, json={"settings": {"auto_reply": False, "sleep_periods": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.post(f"{B}/hypotheses/{a}/activate", headers=AS_CLIENT)

        async def limits(db):
            rows = (await db.scalars(select(CampaignRuntime).order_by(CampaignRuntime.campaign_id))).all()
            return [(r.campaign_id, r.settings["daily_limit"], r.settings["auto_reply"], r.status) for r in rows]
        self.assertEqual(self.run_db(limits), [(a, 1, False, "paused"), (b, 0, False, "paused")])

        # во второй гипотезе уже идёт диалог — её ответы должны читаться и после смены активной
        async def dialog(db):
            lead = await db.scalar(select(Lead).where(Lead.campaign_id == b))
            db.add(CampaignDialog(campaign_id=b, lead_id=lead.id, account_id=21, peer_id=5, access_hash=1, username="two"))
            await db.commit()
        self.run_db(dialog)
        r = self.client.post(f"{B}/start", headers=AS_CLIENT)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.run_db(limits), [(a, 1, False, "running"), (b, 0, False, "running")])
        self.client.post(f"{B}/hypotheses/{b}/activate", headers=AS_CLIENT)
        self.assertEqual([x[1] for x in self.run_db(limits)], [0, 1])
        self.client.post(f"{B}/pause", headers=AS_CLIENT)
        self.assertEqual([x[3] for x in self.run_db(limits)], ["paused", "paused"])

    def test_start_requires_base(self):
        a = self.hyp("Пустая")
        self.client.put(f"{B}/settings", headers=AS_CLIENT, json={"settings": {"auto_reply": False}})
        self.client.post(f"{B}/hypotheses/{a}/activate", headers=AS_CLIENT)
        r = self.client.post(f"{B}/start", headers=AS_CLIENT)
        self.assertEqual(r.status_code, 422)
        self.assertIn("базу", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
