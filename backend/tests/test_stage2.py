"""Audit stage 2: offline conversions, call tracking, AI assistant, web push, client health."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.api.routes.portal import InboundLead
from app.core.access import COOKIE, PORTAL_COOKIE, hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmDeal, CrmStage, OfflineConversion
from app.models.marketing import (AdConnection, ClientWorkspace, LeadInboundSource, PortalProjectAccess, PortalSession,
                                  PortalUser, Project)
from app.models.messaging import MessagingChannel
from app.models.system import AppSetting, PushSubscription
from app.services import ai, conversions, messaging
from app.services.inbound_lead import create_inbound
from app.services.messaging import Incoming, TelegramBot
from app.services.telephony import Mango

ORIGIN = {"Origin": "http://localhost:3000"}


class Stage2Tests(unittest.TestCase):
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
                db.add(ClientWorkspace(id=1, name="Romax"))
                db.add(Project(id=1, workspace_id=1, name="Кухни", is_default=True))
                for uid, role in ((1, "client_owner"), (2, "sales_manager")):
                    db.add(PortalUser(id=uid, workspace_id=1, username=f"u{uid}", display_name=f"User {uid}",
                                      password_hash=hash_password("test-password-123"), role=role, must_change_password=False))
                    db.add(PortalSession(token_hash=token_digest(str(uid) * 43), user_id=uid,
                                         expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.flush()
                db.add(PortalProjectAccess(user_id=2, project_id=1))
                db.add(LeadInboundSource(id=1, workspace_id=1, project_id=1, name="Сайт", token_hash="h1", token_prefix="p1",
                                         active=True, auto_assign=True, auto_accept=True))
                db.add(AdConnection(id=7, workspace_id=1, project_id=1, platform="yandex_direct", name="Директ ROMAX",
                                    external_account_id="romax", access_token_encrypted="x", status="connected"))
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

    def lead(self, name, phone, external, **extra):
        async def go(db):
            payload = InboundLead.model_validate({"external_id": external, "full_name": name, "phone": phone, **extra})
            return await create_inbound(db, await db.get(LeadInboundSource, 1), payload)
        return self.run_db(go)["deal_id"]

    def test_offline_conversions_to_metrica_and_csv(self):
        with_click = self.lead("Анна", "89001112233", "c1", yclid="1234567890123")
        without = self.lead("Борис", "89004445566", "c2")
        qualified = self.run_db(lambda db: db.scalar(select(CrmStage.id).where(CrmStage.analytics_type == "QUALIFIED")))
        for deal_id in (with_click, without):
            self.assertEqual(self.client.post(f"/api/crm/deals/{deal_id}/move", headers=ORIGIN, json={"stage_id": qualified}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/crm/deals/{with_click}/sales", headers=ORIGIN, json={"amount": 250000}).status_code, 201)
        rows = self.run_db(lambda db: db.scalars(select(OfflineConversion).order_by(OfflineConversion.id))).all()
        self.assertEqual([(r.kind, r.status) for r in rows], [("qualified", "pending"), ("qualified", "no_id"), ("sale", "pending")])
        self.assertEqual(float(rows[2].value), 250000)

        calls = []

        async def fake(method, path, token, **kwargs):
            calls.append((method, path, kwargs))
            if path.endswith("/goals") and method == "GET":
                return {"goals": [{"id": 11, "conditions": [{"type": "exact", "url": "stl_qualified"}]}]}
            if path.endswith("/goals"):
                return {"goal": {"id": 12}}
            if path.endswith("/upload"):
                return {"uploading": {"id": 555, "status": "UPLOADED"}}
            return {"counter": {"id": 99, "name": "romax63.ru"}}
        with patch.object(conversions, "metrika", side_effect=fake):
            connected = self.client.put("/api/crm/projects/1/metrika", headers=ORIGIN, json={"counter_id": 99, "token": "y0_" + "x" * 30})
            self.assertEqual(connected.status_code, 200, connected.text)
            self.assertEqual(self.client.post("/api/crm/projects/1/conversions/upload", headers=ORIGIN).json(), {"sent": 2})
        created_goal = [c for c in calls if c[0] == "POST" and c[1].endswith("/goals")]
        self.assertEqual(created_goal[0][2]["json"]["goal"]["conditions"][0]["url"], "stl_sale")
        csv_body = [c for c in calls if c[1].endswith("/upload")][0][2]["files"]["file"][1].decode()
        self.assertIn("ClientId,Yclid,Target,DateTime,Price,Currency", csv_body)
        self.assertIn("1234567890123,stl_sale", csv_body)
        overview = self.client.get("/api/crm/projects/1/conversions").json()
        self.assertEqual(overview["stats"]["qualified"]["sent"], 1)
        self.assertEqual(overview["metrika"]["counter_name"], "romax63.ru")
        export = self.client.get("/api/crm/projects/1/conversions.csv")
        self.assertIn("79004445566", export.text)
        self.as_user(2)
        self.assertEqual(self.client.get("/api/crm/projects/1/conversions.csv").status_code, 403)

    def test_call_tracking_attributes_new_caller_to_ad_cabinet(self):
        users = [{"extension": "101", "name": "User 2", "numbers": []}]
        with patch.object(Mango, "users", AsyncMock(return_value=users)):
            conn = self.client.post("/api/crm/projects/1/telephony", headers=ORIGIN, json={"api_key": "key-123", "api_salt": "salt-456"}).json()
        mapped = self.client.patch(f"/api/crm/telephony/{conn['id']}", headers=ORIGIN,
                                   json={"line_map": {"8 (495) 111-22-33": {"kind": "ad", "id": 7}}})
        self.assertEqual(mapped.status_code, 200, mapped.text)
        self.assertEqual(mapped.json()["line_map"]["74951112233"]["label"], "Директ ROMAX")
        data = {"entry_id": "t1", "call_direction": 1, "from": {"number": "79007770011"}, "to": {"extension": "101"},
                "line_number": "74951112233", "create_time": 1790000000, "talk_time": 1790000005, "end_time": 1790000065, "entry_result": 1}
        form = Mango("key-123", "salt-456").form(data)
        self.client.post(f"{conn['webhook_path']}/events/summary", content=urlencode(form),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        deal = self.run_db(lambda db: db.scalar(select(CrmDeal)))
        self.assertEqual(deal.attribution_snapshot.get("connection_id"), 7)
        log = self.client.get("/api/crm/projects/1/calls").json()
        self.assertEqual(log["items"][0]["channel"], "Директ ROMAX")

    def test_ai_suggest_and_summary(self):
        async def channel(db):
            from app.core.crypto import encrypt_secret
            row = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_bot", name="Бот", status="connected", active=True,
                                   config={}, secret_encrypted=encrypt_secret("123456:ABCDEFGHIJ"))
            db.add(row); await db.commit(); return row.id
        channel_id = self.run_db(channel)

        async def incoming(db):
            row = await db.get(MessagingChannel, channel_id)
            with patch.object(TelegramBot, "poll", AsyncMock(return_value=[Incoming(
                    chat_id="5", title="Ольга", text="Сколько стоит кухня 3 метра?", external_id="m1",
                    sent_at=datetime.now(timezone.utc), direction="in", phone=None, meta={})])):
                await messaging.poll_channel(db, row)
        self.run_db(incoming)
        conversation_id = self.client.get("/api/crm/projects/1/conversations").json()["items"][0]["id"]
        self.assertEqual(self.client.post(f"/api/crm/conversations/{conversation_id}/ai/suggest", headers=ORIGIN).status_code, 422)
        self.client.put("/api/crm/projects/1/ai-settings", headers=ORIGIN, json={"knowledge": "Кухни от 60 000 ₽ за погонный метр", "goal": "записать на замер"})
        complete = AsyncMock(return_value="Ольга, кухня 3 м — от 180 000 ₽. Удобно записать вас на бесплатный замер?")
        with patch.object(ai, "configured", return_value=True), patch.object(ai, "complete", complete):
            detail = self.client.get(f"/api/crm/conversations/{conversation_id}").json()
            self.assertTrue(detail["ai_available"])
            suggestion = self.client.post(f"/api/crm/conversations/{conversation_id}/ai/suggest", headers=ORIGIN).json()
            self.assertIn("замер", suggestion["text"])
            system = complete.await_args.args[0]
            self.assertIn("60 000 ₽", system)
            self.assertIn("записать на замер", system)
            self.assertEqual(complete.await_args.args[1][-1], {"role": "user", "content": "Сколько стоит кухня 3 метра?"})
            summary = self.client.post(f"/api/crm/conversations/{conversation_id}/ai/summary", headers=ORIGIN, json={"save": False}).json()
            self.assertEqual(summary["saved"], False)
        self.as_user(2)
        self.assertEqual(self.client.put("/api/crm/projects/1/ai-settings", headers=ORIGIN, json={"knowledge": ""}).status_code, 403)

    def test_push_subscription(self):
        key = self.client.get("/api/portal/push/key").json()["public_key"]
        self.assertEqual(len(key), 87)  # uncompressed P-256 point, base64url
        self.assertEqual(self.client.get("/api/portal/push/key").json()["public_key"], key)
        sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123456789012345", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
        self.assertEqual(self.client.post("/api/portal/push/subscribe", headers=ORIGIN, json=sub).status_code, 201)
        self.assertEqual(self.client.post("/api/portal/push/subscribe", headers=ORIGIN, json=sub).status_code, 201)
        self.assertEqual(len(self.run_db(lambda db: db.scalars(select(PushSubscription))).all()), 1)
        self.client.post("/api/portal/push/unsubscribe", headers=ORIGIN, json={"endpoint": sub["endpoint"]})
        self.assertEqual(self.run_db(lambda db: db.scalars(select(PushSubscription))).all(), [])
        self.assertIsNotNone(self.run_db(lambda db: db.get(AppSetting, "vapid")))

    def test_client_health(self):
        self.lead("Без ответа", "89001234567", "h1")
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        health = self.client.get("/api/portal/admin/health").json()[0]
        self.assertEqual(health["unanswered_7d"], 1)
        self.assertIn("Без ответа: 1 заявок за неделю", health["reasons"])
        self.assertLess(health["score"], 100)


if __name__ == "__main__":
    unittest.main()
