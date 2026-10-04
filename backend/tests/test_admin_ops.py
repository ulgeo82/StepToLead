"""Deleting / restoring a client company; automatic refresh of ad cabinet statistics."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import test_stage2
from app.core.access import COOKIE
from app.models.marketing import AdConnection, ClientWorkspace, PortalSession, PortalUser, Project
from app.services import ad_sync

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class AdminOpsTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def as_admin(self):
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)

    def test_delete_and_restore_company(self):
        self.assertEqual(self.client.get("/api/crm/plan").status_code, 200)  # client can log in
        self.as_admin()
        names = lambda deleted=False: [w["name"] for w in self.client.get(f"/api/marketing/workspaces{'?deleted=true' if deleted else ''}").json()]
        self.assertEqual(names(), ["Romax"])
        wrong = self.client.post("/api/marketing/workspaces/1/delete", headers=ORIGIN, json={"confirm_name": "Ромакс"})
        self.assertEqual(wrong.status_code, 422)
        done = self.client.post("/api/marketing/workspaces/1/delete", headers=ORIGIN, json={"confirm_name": "romax"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["users"], 2)
        self.assertEqual(names(), [])
        self.assertEqual(names(True), ["Romax · удалена #1"])
        self.assertEqual(self.client.get("/api/portal/admin/health").json(), [])
        state = self.run_db(lambda db: db.get(AdConnection, 7))
        self.assertEqual(state.status, "disconnected")
        self.assertEqual(self.run_db(lambda db: db.get(Project, 1)).status, "archived")
        self.assertEqual(self.run_db(lambda db: db.scalars(select(PortalSession))).all(), [])
        # the name is free for a new client
        self.assertEqual(self.client.post("/api/marketing/workspaces", headers=ORIGIN, json={"name": "Romax"}).status_code, 201)
        self.assertEqual(self.client.post("/api/marketing/workspaces/1/restore", headers=ORIGIN).status_code, 409)
        other = self.run_db(lambda db: db.scalar(select(ClientWorkspace).where(ClientWorkspace.name == "Romax")))
        async def rename(db):
            row = await db.get(ClientWorkspace, other.id); row.name = "Romax 2"; await db.commit()
        self.run_db(rename)
        back = self.client.post("/api/marketing/workspaces/1/restore", headers=ORIGIN)
        self.assertEqual(back.status_code, 200, back.text)
        self.assertEqual(back.json()["name"], "Romax")
        self.assertTrue(all(u.active for u in self.run_db(lambda db: db.scalars(select(PortalUser))).all()))
        self.assertEqual(self.run_db(lambda db: db.get(AdConnection, 7)).status, "connected")
        self.assertEqual(self.run_db(lambda db: db.get(Project, 1)).status, "active")
        self.as_user(1)
        self.assertEqual(self.client.get("/api/crm/plan").status_code, 401)  # sessions were ended; user logs in again

    def test_auto_sync_picks_stale_cabinets(self):
        now = datetime.now(timezone.utc)
        async def seed(db):
            db.add(AdConnection(id=8, workspace_id=1, project_id=1, platform="vk_ads", name="VK", external_account_id="1",
                                access_token_encrypted="x", status="connected", last_synced_at=now - timedelta(minutes=30)))
            db.add(AdConnection(id=9, workspace_id=1, project_id=1, platform="yandex", name="Директ 2", external_account_id="2",
                                access_token_encrypted="x", status="error", last_synced_at=now - timedelta(days=1), last_checked_at=now - timedelta(minutes=5)))
            db.add(AdConnection(id=11, workspace_id=1, project_id=1, platform="yandex", name="Директ", external_account_id="romax",
                                access_token_encrypted="x", status="connected"))
            db.add(AdConnection(id=10, workspace_id=1, project_id=1, platform="avito_items", name="Авито", external_account_id="3",
                                access_token_encrypted="x", status="connected"))
            await db.commit()
        self.run_db(seed)
        self.assertEqual(self.run_db(ad_sync.due), [11])  # Direct never synced; VK fresh; failed one waits; Avito has its own worker
        engine_sessions = self.sessions
        with patch("app.db.SessionLocal", engine_sessions), patch("app.api.routes.marketing.sync_core", AsyncMock(return_value={"ok": True})) as sync:
            self.assertEqual(asyncio.run(ad_sync.run_once()), 1)
        self.assertEqual(sync.await_args.args[1:], (11, None))  # first sync takes the whole history

    def test_lead_triggers_cabinet_refresh(self):
        from app.models.marketing import ClientLead, ClientLeadAttribution
        now = datetime.now(timezone.utc)
        async def seed(db):
            db.add(AdConnection(id=11, workspace_id=1, project_id=1, platform="yandex", name="Директ", external_account_id="romax",
                                access_token_encrypted="x", status="connected", last_synced_at=now - timedelta(minutes=40)))
            db.add(AdConnection(id=12, workspace_id=1, project_id=1, platform="vk_ads", name="VK", external_account_id="v",
                                access_token_encrypted="x", status="connected", last_synced_at=now - timedelta(minutes=5)))
            for cid in (11, 12):
                lead = ClientLead(workspace_id=1, project_id=1, full_name="Анна", status="new", source="Директ")
                db.add(lead); await db.flush()
                db.add(ClientLeadAttribution(lead_id=lead.id, connection_id=cid))
            await db.commit()
        self.run_db(seed)
        self.assertEqual(self.run_db(ad_sync.lead_driven), [11])  # VK synced 5 minutes ago — waits for the 15-minute window
        with patch("app.db.SessionLocal", self.sessions), patch("app.api.routes.marketing.sync_core", AsyncMock(return_value={"ok": True})) as sync:
            asyncio.run(ad_sync.run_once())
        self.assertIn((11, ad_sync.LEAD_DAYS), [c.args[1:] for c in sync.await_args_list])


del Base

if __name__ == "__main__":
    unittest.main()
