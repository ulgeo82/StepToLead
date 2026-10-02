"""Tilda adapter reuses the inbound queue and preserves site/project attribution."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser
from app.models.crm import CrmInbound
from app.models.marketing import ClientWorkspace, Project
from app.models.tilda import TildaConnection, TildaReceipt
from app.models.website import WebsiteSession, WebsiteSite


class TildaTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", role="admin", password_hash=hash_password("test-password-123")))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add_all([ClientWorkspace(id=1, name="One"), ClientWorkspace(id=2, name="Two")])
                db.add_all([Project(id=1, workspace_id=1, name="First", is_default=True),
                            Project(id=2, workspace_id=2, name="Second", is_default=True)])
                db.add(WebsiteSite(id=1, workspace_id=1, project_id=1, name="Tilda", origin="https://one.example",
                                   public_key="site-key-123456789012345678901234", active=True))
                db.add(WebsiteSession(id=1, site_id=1, workspace_id=1, project_id=1,
                    visitor_key="visitor_123456789012", session_key="session_123456789012",
                    utm_source="vk", utm_campaign="real_campaign", landing_url="https://one.example/"))
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
        response = self.client.post("/api/integrations/tilda/connections",
            headers={"Origin": "http://localhost:3000"}, json={"project_id": 1, "website_id": 1})
        self.assertEqual(response.status_code, 201, response.text)
        self.connection = response.json()
        self.webhook = self.connection["webhook_path"]
        self.headers = {self.connection["secret_header"]: self.connection["secret"]}

    def tearDown(self):
        self.client.close()
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def count(self):
        async def read():
            async with self.sessions() as db:
                return await db.scalar(select(func.count()).select_from(CrmInbound))
        return asyncio.run(read())

    def inbound(self, inbound_id):
        async def read():
            async with self.sessions() as db:
                return await db.get(CrmInbound, inbound_id)
        return asyncio.run(read())

    def form(self, **extra):
        return {"tranid": "tilda-123", "formid": "form3645799701", "Name": "Иван Петров",
                "ContactMethod": "Телефон", "Contact": "+79990000000", "Comments": "Перезвоните", **extra}

    def test_probe_secret_and_no_contact(self):
        response = self.client.post(self.webhook, headers=self.headers, data={"test": "test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "ok")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.client.post(self.webhook, headers={"X-StepToLead-Tilda-Secret": "wrong"},
            data=self.form()).status_code, 401)
        self.assertEqual(self.client.post(self.webhook, headers=self.headers,
            data=self.form(Contact="")).status_code, 422)
        self.assertEqual(self.count(), 0)

    def test_create_duplicate_scope_and_sanitization(self):
        data = self.form(website_session_key="session_123456789012", COOKIE="secret-cookie",
                         UTM_MEDIUM="cpc", Website="example.ru")
        response = self.client.post(self.webhook, headers=self.headers, data=data)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.inbound(response.json()["inbound_id"])
        self.assertEqual((row.workspace_id, row.project_id, row.website_session_id), (1, 1, 1))
        self.assertEqual(row.attribution["utm_source"], "vk")
        self.assertEqual(row.attribution["utm_medium"], "cpc")
        self.assertEqual(row.attribution["utm_campaign"], "real_campaign")
        self.assertNotIn("COOKIE", str(row.raw_payload))
        self.assertNotIn(self.connection["secret"], str(row.raw_payload))
        self.assertEqual(row.raw_payload["external_source"], "tilda")
        duplicate = self.client.post(self.webhook, headers=self.headers, data=data)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(duplicate.json()["inbound_id"], row.id)
        self.assertEqual(self.count(), 1)

    def test_multipart_unmatched_session_form_gate_and_status(self):
        response = self.client.post(self.webhook, headers=self.headers,
            files={key: (None, value) for key, value in self.form(website_session_key="missing_session_123456").items()})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.inbound(response.json()["inbound_id"])
        self.assertIsNone(row.website_session_id)
        self.assertEqual(row.attribution, {})
        self.assertEqual(self.client.post(self.webhook, headers=self.headers,
            data=self.form(tranid="other", formid="form-other")).status_code, 422)
        self.assertEqual(self.count(), 1)
        disabled = self.client.patch(f"/api/integrations/tilda/connections/{self.connection['id']}",
            headers={"Origin": "http://localhost:3000"}, json={"is_active": False})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(self.client.post(self.webhook, headers=self.headers,
            data={"test": "test"}).status_code, 404)

    def test_secret_not_listed_and_rotation(self):
        listed = self.client.get("/api/integrations/tilda/connections?project_id=1")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn("secret", listed.json()[0])
        self.assertNotIn("secret_hash", listed.json()[0])
        rotated = self.client.post(f"/api/integrations/tilda/connections/{self.connection['id']}/rotate-secret",
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertNotEqual(rotated.json()["secret"], self.connection["secret"])
        self.assertEqual(self.client.post(self.webhook, headers=self.headers, data={"test": "test"}).status_code, 401)
        self.assertEqual(self.client.post(self.webhook,
            headers={"X-StepToLead-Tilda-Secret": rotated.json()["secret"]}, data={"test": "test"}).status_code, 200)

    def test_other_contact_and_site_scope(self):
        wrong_site = self.client.post("/api/integrations/tilda/connections",
            headers={"Origin": "http://localhost:3000"}, json={"project_id": 2, "website_id": 1})
        self.assertEqual(wrong_site.status_code, 404)
        accepted = self.client.post(self.webhook, headers=self.headers,
            data=self.form(tranid="telegram-contact", Contact="@ivan_example", ContactMethod="Telegram"))
        self.assertEqual(accepted.status_code, 200, accepted.text)
        row = self.inbound(accepted.json()["inbound_id"])
        self.assertIsNone(row.phone)
        self.assertIsNone(row.email)
        self.assertEqual(row.raw_payload["contact"], "@ivan_example")
        self.assertFalse(row.raw_payload["contact_consent"])

    def test_reported_delivery_persisted_once_in_project(self):
        data = {"tranid": "8713176482", "formid": "3645799701", "Name": "тест",
                "contact": "@test9999999", "ContactMethod": "Telegram", "comment": "переписал",
                "website_session_key": "0e22a680-492a-4613-9d2d-a8cb678b21b9"}
        response = self.client.post(self.webhook, headers=self.headers, data=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["project_id"], 1)
        self.assertFalse(response.json()["duplicate"])
        row = self.inbound(response.json()["lead_id"])
        self.assertEqual((row.workspace_id, row.project_id), (1, 1))
        self.assertEqual(row.raw_payload["contact"], data["contact"])
        self.assertEqual(row.raw_payload["website_session_key"], data["website_session_key"])
        self.assertEqual(row.raw_payload["source"], "tilda")
        repeated = self.client.post(self.webhook, headers=self.headers, data=data)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertTrue(repeated.json()["duplicate"])
        self.assertEqual(repeated.json()["lead_id"], row.id)
        self.assertEqual(repeated.json()["project_id"], 1)
        self.assertEqual(self.count(), 1)

    def test_commit_failure_is_500_and_retry_creates_request(self):
        with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new_callable=AsyncMock,
                   side_effect=RuntimeError("diagnostic commit failure")):
            with self.assertLogs("uvicorn.error.tilda", level="ERROR") as logs:
                failed = self.client.post(self.webhook, headers=self.headers, data=self.form())
            self.assertEqual(failed.status_code, 500, failed.text)
            self.assertIn("diagnostic commit failure", " ".join(logs.output))
        self.assertEqual(self.count(), 0)
        async def receipts():
            async with self.sessions() as db:
                return await db.scalar(select(func.count()).select_from(TildaReceipt))
        self.assertEqual(asyncio.run(receipts()), 0)
        retry = self.client.post(self.webhook, headers=self.headers, data=self.form())
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertFalse(retry.json()["duplicate"])
        self.assertEqual(self.count(), 1)

    def test_mismatched_source_scope_fails_without_receipt(self):
        async def corrupt():
            from app.models.marketing import LeadInboundSource
            async with self.sessions() as db:
                connection = await db.get(TildaConnection, self.connection["id"])
                source = await db.get(LeadInboundSource, connection.inbound_source_id)
                source.project_id = 2
                await db.commit()
        asyncio.run(corrupt())
        response = self.client.post(self.webhook, headers=self.headers, data=self.form())
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.count(), 0)

    def test_telegram_visible_in_queue_and_preserved_on_accept(self):
        from app.core.access import PORTAL_COOKIE
        from app.models.marketing import PortalUser, PortalSession, ClientLead
        async def owner():
            async with self.sessions() as db:
                db.add(PortalUser(id=1, workspace_id=1, username="owner", display_name="Owner",
                    password_hash=hash_password("test-password-123"), role="client_owner", must_change_password=False))
                db.add(PortalSession(token_hash=token_digest("p" * 43), user_id=1,
                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.commit()
        asyncio.run(owner())
        response = self.client.post(self.webhook, headers=self.headers,
            data=self.form(Contact="@test9999999", ContactMethod="Telegram"))
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.clear()
        self.client.cookies.set(PORTAL_COOKIE, "p" * 43)
        listed = self.client.get("/api/crm/projects/1/inbound")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"][0]["raw_payload"]["contact"], "@test9999999")
        accepted = self.client.post(f"/api/crm/inbound/{response.json()['inbound_id']}/accept",
            headers={"Origin": "http://localhost:3000"}, json={})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        async def read():
            async with self.sessions() as db:
                return await db.scalar(select(ClientLead))
        lead = asyncio.run(read())
        self.assertEqual(lead.telegram, "@test9999999")
        self.assertEqual(lead.project_id, 1)

        deal_id = accepted.json()["deal_id"]
        contacts = self.client.get("/api/crm/projects/1/contacts").json()["items"]
        self.assertEqual(contacts[0]["telegram"], "@test9999999")

        # Older accepted requests can have no method or stored Telegram.
        async def legacy_contact():
            from app.models.crm import CrmContact
            async with self.sessions() as db:
                contact = await db.get(CrmContact, contacts[0]["id"])
                contact.telegram = None
                inbound = await db.get(CrmInbound, response.json()["inbound_id"])
                inbound.raw_payload = {**inbound.raw_payload, "contact_method": None}
                await db.commit()
        asyncio.run(legacy_contact())
        self.assertEqual(self.client.get("/api/crm/projects/1/contacts").json()["items"][0]["telegram"], "@test9999999")
        detail = self.client.get(f"/api/crm/deals/{deal_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["contact"]["telegram"], "@test9999999")

        headers = {"Origin": "http://localhost:3000"}
        archived = self.client.post(f"/api/crm/deals/{deal_id}/archive", headers=headers)
        self.assertEqual(archived.status_code, 200, archived.text)
        def board_ids(archive=False):
            result = self.client.get(f"/api/crm/projects/1/board?archived={str(archive).lower()}")
            self.assertEqual(result.status_code, 200, result.text)
            return [deal["id"] for column in result.json()["columns"] for deal in column["deals"]]
        self.assertNotIn(deal_id, board_ids())
        self.assertIn(deal_id, board_ids(True))
        restored = self.client.post(f"/api/crm/deals/{deal_id}/restore", headers=headers)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIn(deal_id, board_ids())
        self.assertNotIn(deal_id, board_ids(True))
        self.assertEqual(self.count(), 1)

    def test_telegram_inference_only_for_unambiguous_contact(self):
        from app.api.routes.crm import inbound_telegram
        self.assertEqual(inbound_telegram({"external_source": "tilda", "contact": "@test9999999"}), "@test9999999")
        self.assertIsNone(inbound_telegram({"external_source": "tilda", "contact": "test@example.com"}))
        self.assertIsNone(inbound_telegram({"external_source": "tilda", "contact": "@test9999999", "contact_method": "Email"}))
        self.assertIsNone(inbound_telegram({"external_source": "other", "contact": "@test9999999"}))
