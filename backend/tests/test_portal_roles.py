"""Role and tenant checks use an isolated SQLite database."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace


class PortalRoleTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def initialize():
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", password_hash=hash_password("test-password-123"), role="admin"))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                db.add_all([ClientWorkspace(id=1, name="Client One"), ClientWorkspace(id=2, name="Client Two")])
                await db.commit()
        asyncio.run(initialize())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api"); app.dependency_overrides[get_db] = database
        self.client = TestClient(app); self.origin = {"Origin": "http://localhost:3000"}
        self.limiter = patch("app.api.routes.portal.rate_limit", new=AsyncMock()); self.limiter.start()

    def tearDown(self):
        self.client.close(); self.limiter.stop(); asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def test_admin_creates_user_and_portal_session_is_tenant_scoped(self):
        self.client.cookies.set("stl_session", "a" * 43)
        created = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "owner", "display_name": "Owner One", "role": "client_owner",
        })
        self.assertEqual(created.status_code, 201, created.text)
        password = created.json()["temporary_password"]
        second = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "manager", "display_name": "Manager One", "role": "sales_manager",
        })
        self.assertEqual(second.status_code, 201, second.text)
        updated = self.client.patch(f"/api/portal/admin/users/{second.json()['id']}", headers=self.origin, json={
            "role": "sales_head", "active": False,
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["role"], "sales_head")
        self.assertFalse(updated.json()["active"])
        reset = self.client.post(f"/api/portal/admin/users/{second.json()['id']}/reset-password", headers=self.origin)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertTrue(reset.json()["temporary_password"])
        self.assertTrue(reset.json()["must_change_password"])
        members = self.client.get("/api/portal/admin/users?workspace_id=1")
        self.assertEqual(len(members.json()), 2)
        self.client.cookies.clear()
        logged_in = self.client.post("/api/portal/auth/login", headers=self.origin, json={"username": "owner", "password": password})
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        dashboard = self.client.get("/api/portal/dashboard")
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["user"]["workspace_id"], 1)
        self.assertEqual(dashboard.json()["user"]["workspace_name"], "Client One")

    def test_portal_user_cannot_use_admin_api(self):
        self.assertEqual(self.client.get("/api/portal/dashboard").status_code, 401)
        self.assertEqual(self.client.get("/api/portal/admin/users").status_code, 401)

    def test_crm_is_tenant_scoped_and_manager_sees_only_assigned_leads(self):
        self.client.cookies.set("stl_session", "a" * 43)
        created = []
        for username, name, role, workspace_id in [
            ("head", "Sales Head", "sales_head", 1),
            ("manager", "Manager One", "sales_manager", 1),
            ("other", "Other Manager", "sales_manager", 2),
        ]:
            response = self.client.post("/api/portal/admin/users", headers=self.origin, json={
                "workspace_id": workspace_id, "username": username, "display_name": name, "role": role,
            })
            self.assertEqual(response.status_code, 201, response.text); created.append(response.json())
        head, manager, other = created
        self.client.cookies.clear()
        login = self.client.post("/api/portal/auth/login", headers=self.origin, json={"username": "head", "password": head["temporary_password"]})
        self.assertEqual(login.status_code, 200, login.text)
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin, json={
            "full_name": "Test Lead", "phone": "+70000000000", "source": "Яндекс Директ", "assigned_to_id": manager["id"],
        })
        self.assertEqual(lead.status_code, 201, lead.text)
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={"username": "manager", "password": manager["temporary_password"]})
        rows = self.client.get("/api/portal/crm/leads")
        self.assertEqual(len(rows.json()), 1)
        updated = self.client.patch(f"/api/portal/crm/leads/{lead.json()['id']}", headers=self.origin, json={"status": "contacted"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["status"], "contacted")
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={"username": "other", "password": other["temporary_password"]})
        self.assertEqual(self.client.get("/api/portal/crm/leads").json(), [])
        self.assertEqual(self.client.get(f"/api/portal/crm/leads/{lead.json()['id']}/events").status_code, 404)

    def test_inbound_webhook_deduplicates_and_assigns_manager(self):
        self.client.cookies.set("stl_session", "a" * 43)
        manager = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "webhook-manager", "display_name": "Webhook Manager", "role": "sales_manager",
        }).json()
        source = self.client.post("/api/portal/admin/lead-sources", headers=self.origin, json={
            "workspace_id": 1, "name": "Website", "auto_assign": True,
        })
        self.assertEqual(source.status_code, 201, source.text)
        token = source.json()["webhook_token"]
        self.client.cookies.clear()
        first = self.client.post(f"/api/portal/inbound/{token}", json={
            "external_id": "form-42", "full_name": "Website Lead", "phone": "+7 (900) 000-00-00", "source": "Форма сайта",
            "external_campaign_id": "campaign-77", "external_ad_id": "ad-9",
            "utm_source": "yandex", "utm_medium": "cpc", "utm_campaign": "search-test",
            "landing_url": "https://example.test/landing",
        })
        self.assertEqual(first.status_code, 202, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(first.json()["assigned_to_id"], manager["id"])
        duplicate = self.client.post(f"/api/portal/inbound/{token}", json={
            "external_id": "form-42", "full_name": "Website Lead", "phone": "+7 (900) 000-00-00",
        })
        self.assertEqual(duplicate.status_code, 202, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        login = self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "webhook-manager", "password": manager["temporary_password"],
        })
        self.assertEqual(login.status_code, 200, login.text)
        rows = self.client.get("/api/portal/crm/leads")
        self.assertEqual(len(rows.json()), 1)
        self.assertEqual(rows.json()[0]["phone"], "+79000000000")
        attribution = self.client.get(f"/api/portal/crm/leads/{first.json()['lead_id']}/attribution")
        self.assertEqual(attribution.status_code, 200, attribution.text)
        self.assertEqual(attribution.json()["external_campaign_id"], "campaign-77")
        self.assertEqual(attribution.json()["utm_source"], "yandex")
        self.client.cookies.set("stl_session", "a" * 43)
        revoked = self.client.delete(f"/api/portal/admin/lead-sources/{source.json()['id']}", headers=self.origin)
        self.assertEqual(revoked.status_code, 204, revoked.text)
        rejected = self.client.post(f"/api/portal/inbound/{token}", json={
            "external_id": "form-43", "full_name": "Rejected Lead", "phone": "+79000000001",
        })
        self.assertEqual(rejected.status_code, 404, rejected.text)


if __name__ == "__main__":
    unittest.main()
