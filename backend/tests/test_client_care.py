"""Phase 1 of the portal audit: lead quality, trusted sources, 15-minute SLA, weekly report, launch screen."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.api.routes.portal import InboundLead
from app.core.access import COOKIE, PORTAL_COOKIE, hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmDeal, CrmInbound
from app.models.marketing import (ClientWorkspace, LeadInboundSource, PortalNotification, PortalProjectAccess,
                                  PortalSession, PortalUser, Project)
from app.services import watchdog
from app.services.inbound_lead import create_inbound

ORIGIN = {"Origin": "http://localhost:3000"}


class ClientCareTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", role="admin", password_hash=hash_password("test-password-123")))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1, expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add(ClientWorkspace(id=1, name="One"))
                db.add(Project(id=1, workspace_id=1, name="Кухни", is_default=True))
                for uid, role in ((1, "client_owner"), (2, "sales_manager")):
                    db.add(PortalUser(id=uid, workspace_id=1, username=f"u{uid}", display_name=f"User {uid}",
                                      password_hash=hash_password("test-password-123"), role=role, must_change_password=False))
                    db.add(PortalSession(token_hash=token_digest(str(uid) * 43), user_id=uid,
                                         expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.flush()
                db.add(PortalProjectAccess(user_id=2, project_id=1))
                db.add_all([LeadInboundSource(id=1, workspace_id=1, project_id=1, name="Сайт", token_hash="h1", token_prefix="p1",
                                              active=True, auto_assign=True, auto_accept=True),
                            LeadInboundSource(id=2, workspace_id=1, project_id=1, name="Чаты", token_hash="h2", token_prefix="p2",
                                              active=True, auto_assign=True, auto_accept=False)])
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.as_user(1)

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def as_user(self, uid):
        self.client.cookies.clear(); self.client.cookies.set(PORTAL_COOKIE, str(uid) * 43)

    def run_db(self, fn):
        async def run():
            async with self.sessions() as db:
                return await fn(db)
        return asyncio.run(run())

    def lead(self, source_id, name, phone, external):
        async def go(db):
            source = await db.get(LeadInboundSource, source_id)
            payload = InboundLead.model_validate({"external_id": external, "full_name": name, "phone": phone, "source": "site"})
            return await create_inbound(db, source, payload)
        return self.run_db(go)

    def test_trusted_source_creates_deal_and_repeat_joins_it(self):
        first = self.lead(1, "Анна", "89001112233", "f1")
        self.assertEqual(first["status"], "ACCEPTED")
        self.assertIsNotNone(first["deal_id"])
        second = self.lead(1, "Анна снова", "+7 900 111-22-33", "f2")
        self.assertEqual(second["deal_id"], first["deal_id"])
        self.assertEqual(self.run_db(lambda db: db.scalar(select(CrmDeal.id).where(CrmDeal.id != first["deal_id"]))), None)
        events = [a["event_type"] for a in self.client.get(f"/api/crm/deals/{first['deal_id']}").json()["activities"]]
        self.assertIn("INBOUND_REPEAT", events)
        queued = self.lead(2, "Чат", "89005556677", "c1")
        self.assertEqual((queued["status"], queued["deal_id"]), ("NEW", None))
        integrations = self.client.get("/api/crm/projects/1/integrations").json()
        self.assertEqual({s["id"]: s["auto_accept"] for s in integrations["sources"]}, {1: True, 2: False})
        patched = self.client.patch("/api/crm/inbound-sources/2", headers=ORIGIN, json={"auto_accept": True})
        self.assertEqual(patched.json()["auto_accept"], True)
        self.as_user(2)
        self.assertEqual(self.client.patch("/api/crm/inbound-sources/2", headers=ORIGIN, json={"auto_accept": False}).status_code, 403)

    def test_quality_mark_feeds_analytics(self):
        deals = [self.lead(1, f"Клиент {i}", f"8900000000{i}", f"q{i}")["deal_id"] for i in range(3)]
        self.assertEqual(self.client.post(f"/api/crm/deals/{deals[0]}/quality", headers=ORIGIN,
                                          json={"quality": "non_target"}).status_code, 422)
        self.client.post(f"/api/crm/deals/{deals[0]}/quality", headers=ORIGIN, json={"quality": "non_target", "reason": "Не та услуга"})
        self.client.post(f"/api/crm/deals/{deals[1]}/quality", headers=ORIGIN, json={"quality": "target"})
        detail = self.client.get(f"/api/crm/deals/{deals[0]}").json()
        self.assertEqual((detail["quality"], detail["quality_reason"]), ("non_target", "Не та услуга"))

        async def facts(db):
            from app.services.result_analytics import result_facts
            today = datetime.now(timezone.utc).date()
            return await result_facts(db, await db.get(Project, 1), today - timedelta(days=1), today)
        totals = self.run_db(facts)["current"]["totals"]
        self.assertEqual((totals["target"], totals["non_target"], totals["target_share"]), (1, 1, 50.0))

    def test_sla_escalation_once_and_only_in_working_hours(self):
        queued = self.lead(2, "Ночной", "89007778899", "n1")
        deal = self.lead(1, "Сайт", "89001234500", "s1")

        async def age(db):
            old = datetime.now(timezone.utc) - timedelta(minutes=20)
            (await db.get(CrmInbound, queued["inbound_id"])).received_at = old
            (await db.get(CrmDeal, deal["deal_id"])).created_at = old
            await db.commit()
        self.run_db(age)

        async def check(db):
            return await watchdog.check_sla(db)
        with patch.object(watchdog, "working_hours", return_value=False):
            self.assertEqual(self.run_db(check), 0)
        with patch.object(watchdog, "working_hours", return_value=True):
            self.assertEqual(self.run_db(check), 2)
            self.assertEqual(self.run_db(check), 0)
        titles = self.run_db(lambda db: db.scalars(select(PortalNotification.title))).all()
        self.assertIn("Заявка ждёт ответа", titles)
        self.assertIn("Заявка без ответа", titles)

    def test_weekly_report_lines(self):
        self.lead(1, "Анна", "89001112233", "w1")

        async def report(db):
            return await watchdog.weekly_report(db, await db.get(Project, 1), datetime.now(timezone.utc).date())
        title, lines = self.run_db(report)
        self.assertTrue(title.startswith("Неделя"))
        self.assertTrue(any(line.startswith("Заявки: 1") for line in lines))
        self.assertTrue(any("Без ответа до сих пор: 1" == line for line in lines))

    def test_launch_screen(self):
        launch = self.client.get("/api/crm/projects/1/launch").json()
        keys = {i["key"]: i["done"] for i in launch["checklist"]}
        self.assertEqual((keys["team"], keys["first_lead"], keys["telegram"]), (True, False, False))
        self.assertEqual(len(launch["plan"]), 6)
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        saved = self.client.put("/api/portal/admin/projects/1/launch", headers=ORIGIN, json={
            "marketer": {"name": "Мария", "telegram": "@maria_stl"},
            "plan": [{"title": "Аудит ниши", "status": "done"}, {"title": "Запуск Директа", "status": "doing",
                                                                 "due": str(date.today())}],
            "goals": {"leads": 40, "cpl": 1500}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.as_user(2)
        launch = self.client.get("/api/crm/projects/1/launch").json()
        self.assertEqual(launch["marketer"]["name"], "Мария")
        self.assertEqual([p["status"] for p in launch["plan"]], ["done", "doing"])
        self.assertEqual({i["key"] for i in launch["checklist"]}, {"telegram", "first_lead"})
        self.assertEqual(self.client.put("/api/crm/projects/1/launch/goals", headers=ORIGIN, json={"leads": 1}).status_code, 403)
        self.client.post("/api/crm/projects/1/launch/dismiss", headers=ORIGIN)
        self.assertTrue(self.client.get("/api/crm/projects/1/launch").json()["dismissed"])
        self.as_user(1)
        self.assertFalse(self.client.get("/api/crm/projects/1/launch").json()["dismissed"])
        goals = self.client.put("/api/crm/projects/1/launch/goals", headers=ORIGIN, json={"leads": 50, "sales": 5}).json()
        self.assertEqual(goals["goals"], {"leads": 50, "sales": 5})


if __name__ == "__main__":
    unittest.main()
