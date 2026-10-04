"""Notification recipients are configurable per project and limited to project members."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.marketing import (ClientWorkspace, PortalNotification, PortalProjectAccess, PortalUser, Project,
                                  ProjectNotificationRule)
from app.services import notifications
from app.services.notifications import PENDING_KEY, notify


class NotificationRecipientTests(unittest.TestCase):
    def setUp(self):
        async def no_rate_limit(*_args, **_kwargs):
            return None
        self.rate_limit_patch = patch("app.api.routes.portal.rate_limit", no_rate_limit)
        self.rate_limit_patch.start()
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", role="admin", password_hash=hash_password("test-password-123")))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add(ClientWorkspace(id=1, name="One"))
                db.add_all([Project(id=1, workspace_id=1, name="First", is_default=True),
                            Project(id=2, workspace_id=1, name="Other")])
                users = [(10, "owner", "client_owner"), (11, "head", "sales_head"), (12, "manager", "sales_manager"),
                         (13, "outsider", "sales_head"), (14, "marketer", "client_marketer")]
                for uid, name, role in users:
                    db.add(PortalUser(id=uid, workspace_id=1, username=name, password_hash="x", display_name=name,
                                      role=role, active=True, must_change_password=False))
                await db.flush()
                # 13 is a sales head of project 2 only; it must never receive project 1 events.
                db.add_all([PortalProjectAccess(user_id=11, project_id=1), PortalProjectAccess(user_id=12, project_id=1),
                            PortalProjectAccess(user_id=14, project_id=1), PortalProjectAccess(user_id=13, project_id=2)])
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.origin = {"Origin": "http://localhost:3000"}
        self.client.cookies.set("stl_session", "a" * 43)

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)
        self.rate_limit_patch.stop()

    def fire(self, **kwargs):
        async def run():
            async with self.sessions() as db:
                count = await notify(db, 1, "new_lead", "Новая заявка", "Анна · Tilda", **kwargs)
                pending = list(db.sync_session.info.get(PENDING_KEY, []))
                await db.commit()
                ids = sorted((await db.scalars(select(PortalNotification.user_id))).all())
                await db.execute(PortalNotification.__table__.delete()); await db.commit()
                return count, ids, pending
        return asyncio.run(run())

    def set_rule(self, **values):
        async def run():
            async with self.sessions() as db:
                db.add(ProjectNotificationRule(project_id=1, event_key="new_lead", **values)); await db.commit()
        asyncio.run(run())

    def test_default_audience_is_project_owner_and_heads(self):
        count, ids, _ = self.fire()
        self.assertEqual((count, ids), (2, [10, 11]))
        self.assertEqual(self.fire(assignee_id=12)[1], [12])
        self.assertEqual(self.fire(actor_id=10)[1], [11])

    def test_selected_members_assignee_and_disabled_rule(self):
        self.set_rule(enabled=True, in_app=True, recipient_user_ids=[14, 13], notify_assignee=True)
        self.assertEqual(self.fire()[1], [14])
        self.assertEqual(self.fire(assignee_id=12)[1], [12, 14])

        async def disable():
            async with self.sessions() as db:
                row = await db.scalar(select(ProjectNotificationRule)); row.enabled = False; await db.commit()
        asyncio.run(disable())
        self.assertEqual(self.fire(), (0, [], []))

    def test_telegram_only_for_linked_users_when_bot_configured(self):
        self.set_rule(enabled=True, in_app=False, telegram=True, recipient_user_ids=[11, 12])

        async def link():
            async with self.sessions() as db:
                (await db.get(PortalUser, 11)).telegram_chat_id = "555"; await db.commit()
        asyncio.run(link())
        with patch.object(notifications.settings, "telegram_bot_token", ""):
            self.assertEqual(self.fire()[2], [])
        with patch.object(notifications.settings, "telegram_bot_token", "123:abc"):
            count, ids, pending = self.fire(details=["Способ связи: WhatsApp"])
        self.assertEqual((count, ids), (2, []))
        self.assertEqual([message["chat_id"] for message in pending], ["555"])
        self.assertIn("WhatsApp", pending[0]["text"])

    def test_settings_api_validates_recipients(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "api-owner", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "api-owner", "password": owner["temporary_password"]})
        data = self.client.get("/api/settings?project_id=1").json()
        self.assertEqual({m["id"] for m in data["members"]}, {10, 11, 12, 14, owner["id"]})
        rule = next(r for r in data["notifications"] if r["key"] == "new_lead")
        self.assertIsNone(rule["recipient_user_ids"])
        self.assertEqual(self.client.patch("/api/settings/notifications/new_lead?project_id=1", headers=self.origin,
            json={"enabled": True, "recipient_user_ids": [13]}).status_code, 422)
        saved = self.client.patch("/api/settings/notifications/new_lead?project_id=1", headers=self.origin,
            json={"enabled": True, "in_app": True, "telegram": True, "recipient_user_ids": [12, 14],
                  "notify_assignee": False})
        self.assertEqual(saved.status_code, 200, saved.text)
        rule = next(r for r in self.client.get("/api/settings?project_id=1").json()["notifications"]
                    if r["key"] == "new_lead")
        self.assertEqual((rule["recipient_user_ids"], rule["telegram"], rule["notify_assignee"]), ([12, 14], True, False))


if __name__ == "__main__":
    unittest.main()
