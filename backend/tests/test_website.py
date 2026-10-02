"""Site collection must preserve project boundaries and never synthesize leads."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.marketing import ClientWorkspace, Project


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        async def no_rate_limit(*_args, **_kwargs):
            return None
        self.rate_limit_patch = patch("app.api.routes.access.rate_limit", no_rate_limit)
        self.rate_limit_patch.start()
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", role="admin", password_hash=hash_password("test-password-123")))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add_all([ClientWorkspace(id=1, name="One"), ClientWorkspace(id=2, name="Two")])
                db.add_all([Project(id=1, workspace_id=1, name="First", is_default=True),
                            Project(id=2, workspace_id=2, name="Second", is_default=True)])
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI()
        app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set("stl_session", "a" * 43)
        self.frontend_origin = {"Origin": "http://localhost:3000"}

    def tearDown(self):
        self.client.close()
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)
        self.rate_limit_patch.stop()

    def test_site_scope_consent_event_and_no_fake_lead(self):
        created = self.client.post("/api/website/sites", headers=self.frontend_origin,
            json={"project_id": 1, "name": "Тестовый сайт", "origin": "https://one.example"})
        self.assertEqual(created.status_code, 201, created.text)
        site = created.json()
        payload = {"visitor": "visitor_123456789012", "session": "session_123456789012",
                   "events": [{"key": "event_12345678901234", "name": "page_view",
                               "page": "https://one.example/?email=private@example.com"},
                              {"key": "event_12345678901235", "name": "cta_click",
                               "page": "https://one.example/", "element_name": "Обсудить проект"}]}
        with patch("app.api.routes.website.rate_limit", return_value=None):
            forbidden = self.client.post(f"/api/website/collect/{site['public_key']}",
                headers={"Origin": "https://other.example", "Content-Type": "text/plain"}, json=payload)
            self.assertEqual(forbidden.status_code, 403)
            accepted = self.client.post(f"/api/website/collect/{site['public_key']}",
                headers={"Origin": "https://one.example", "Content-Type": "text/plain"}, json=payload)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            repeated = self.client.post(f"/api/website/collect/{site['public_key']}",
                headers={"Origin": "https://one.example", "Content-Type": "text/plain"}, json=payload)
            self.assertEqual(repeated.status_code, 202, repeated.text)
        report = self.client.get("/api/website/analytics?project_id=1")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["totals"]["sessions"], 1)
        self.assertEqual(report.json()["totals"]["leads"], 0)
        self.assertEqual(report.json()["pages"], [{"path": "/", "views": 1}])
        self.assertEqual(report.json()["funnel"]["cta_click"], 1)
        self.assertEqual(report.json()["actions"][0]["clicks"], 1)
        self.assertEqual(self.client.get("/api/website/analytics?project_id=2").json()["totals"]["sessions"], 0)

        inbound_source = self.client.post("/api/portal/admin/lead-sources", headers=self.frontend_origin,
            json={"workspace_id": 1, "name": "Форма сайта", "auto_assign": True})
        self.assertEqual(inbound_source.status_code, 201, inbound_source.text)
        queued = self.client.post(f"/api/portal/inbound/{inbound_source.json()['webhook_token']}", json={
            "external_id": "form-website-1", "full_name": "Иван Петров", "phone": "+79000000000",
            "website_session_key": payload["session"]})
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(self.client.get("/api/website/analytics?project_id=1").json()["totals"]["leads"], 0)

        owner = self.client.post("/api/portal/admin/users", headers=self.frontend_origin,
            json={"workspace_id": 1, "username": "website-owner", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        login = self.client.post("/api/portal/auth/login", headers=self.frontend_origin,
            json={"username": "website-owner", "password": owner["temporary_password"]})
        self.assertEqual(login.status_code, 200, login.text)
        accepted = self.client.post(f"/api/crm/inbound/{queued.json()['inbound_id']}/accept",
            headers=self.frontend_origin, json={"responsible_user_id": owner["id"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.client.get("/api/website/analytics?project_id=1").json()["totals"]["leads"], 1)
        sale = self.client.post(f"/api/crm/deals/{accepted.json()['deal_id']}/sales",
            headers=self.frontend_origin, json={"amount": 120000})
        self.assertEqual(sale.status_code, 201, sale.text)
        website_totals = self.client.get("/api/website/analytics?project_id=1").json()["totals"]
        result_totals = self.client.get("/api/result?project_id=1").json()["current"]["totals"]
        self.assertEqual(website_totals["sales"], 1)
        self.assertEqual(website_totals["revenue"], 120000)
        action = self.client.get("/api/website/analytics?project_id=1").json()["actions"][0]
        self.assertEqual(action["leads"], 1)
        self.assertEqual(action["revenue"], 120000)
        self.assertEqual(result_totals["sales"], 1)
        self.assertEqual(result_totals["revenue"], 120000)
        self.assertEqual(self.client.get("/api/website/sites?project_id=2").status_code, 404)
        self.assertEqual(self.client.get("/api/website/analytics?project_id=2").status_code, 404)
