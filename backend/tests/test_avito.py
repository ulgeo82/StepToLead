"""Avito classifieds/ads parsing, lead import from chats/calls and attribution to the cabinet."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import PORTAL_COOKIE, hash_password, token_digest
from app.db import Base, get_db
from app.models.crm import CrmInbound
from app.models.marketing import (AdConnection, ClientLeadAttribution, ClientWorkspace, PortalSession, PortalUser,
                                  Project)
from app.services import avito, avito_leads

NOW = datetime.now(timezone.utc)


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def request(self, method, path, *, json=None, params=None, allow=()):
        self.calls.append((method, path, json, params))
        for prefix, handler in self.routes.items():
            if path.startswith(prefix):
                status, body = handler(json, params) if callable(handler) else handler
                if status >= 400 and status not in allow:
                    raise avito.AvitoError(f"HTTP {status}", status)
                return status, body
        raise AssertionError(f"unexpected Avito call {method} {path}")

    async def get(self, path, **kwargs):
        return (await self.request("GET", path, **kwargs))[1]

    async def post(self, path, payload, **kwargs):
        return (await self.request("POST", path, json=payload, **kwargs))[1]


class AvitoParsingTests(unittest.TestCase):
    def test_items_daily_metrics_use_kopecks_and_day_buckets(self):
        day = datetime(2026, 9, 1, tzinfo=timezone.utc)
        client = FakeClient({"/stats/v2/accounts/42/items": (200, {"result": {"dataTotalCount": 1, "groupings": [
            {"id": int(day.timestamp()) - 3 * 3600, "type": "day", "metrics": [  # Moscow midnight
                {"slug": "views", "value": 120}, {"slug": "impressions", "value": 900},
                {"slug": "contacts", "value": 7}, {"slug": "spending", "value": 123456},
                {"slug": "spendingBonus", "value": 5000}]}]}})})
        row = AdConnection(id=1, platform=avito.ITEMS, external_account_id="42", last_synced_at=None)
        rows = asyncio.run(avito.items_metrics(client, row))
        self.assertEqual(rows[0]["date"], date(2026, 9, 1))
        self.assertEqual(rows[0]["spend"], Decimal("1234.56"))
        self.assertEqual((rows[0]["impressions"], rows[0]["clicks"], rows[0]["leads"]), (900, 120, 7))
        sent = client.calls[0][2]
        self.assertEqual(sent["grouping"], "day")
        self.assertEqual(date.fromisoformat(sent["dateTo"]) - date.fromisoformat(sent["dateFrom"]), timedelta(days=269))

    def test_ads_metrics_split_into_100_day_chunks_and_skip_drafts(self):
        stats_calls = []

        def stats(payload, _params):
            stats_calls.append(payload)
            return 200, {"campaign": {"data": [{"timestamp": f"{payload['dateFrom']}T00:00:00Z", "views": 1000,
                                                 "clicks": 20, "spend": 150, "spendKopeks": 15050,
                                                 "spendBonus": 10, "spendBonusKopeks": 1000}]}}
        client = FakeClient({"/ads/v1/account/77/campaigns/": stats,
                             "/ads/v1/account/77/campaigns": (200, {"total": 2, "campaigns": [
                                 {"id": 5, "name": "Баннеры", "status": "active", "paymentModel": "CPM"},
                                 {"id": 6, "name": "Черновик", "status": "draft"}]})})
        row = AdConnection(id=2, platform=avito.ADS, external_account_id="77", last_synced_at=None)
        daily, campaigns = asyncio.run(avito.ads_metrics(client, row))
        self.assertEqual(len(stats_calls), 4)  # 365 days -> 4 requests of <= 100 days, draft skipped
        for payload in stats_calls:
            self.assertLess((date.fromisoformat(payload["dateTo"]) - date.fromisoformat(payload["dateFrom"])).days, 100)
        self.assertEqual(daily[0]["spend"], Decimal("150.50"))
        self.assertEqual(daily[0]["campaigns"][0]["external_campaign_id"], "5")
        self.assertEqual(daily[0]["raw"]["bonus"], "10.00")
        self.assertEqual({item["id"] for item in campaigns}, {"5", "6"})

    def test_phone_normalization(self):
        self.assertEqual(avito_leads.normalize_phone("89444988703"), "+79444988703")
        self.assertEqual(avito_leads.normalize_phone("+7 (999) 000-00-00"), "+79990000000")
        self.assertIsNone(avito_leads.normalize_phone("123"))


class AvitoLeadImportTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=1, name="One"))
                db.add(Project(id=1, workspace_id=1, name="First", is_default=True))
                db.add(PortalUser(id=1, workspace_id=1, username="owner", display_name="Owner",
                                  password_hash=hash_password("test-password-123"), role="client_owner",
                                  must_change_password=False))
                db.add(PortalSession(token_hash=token_digest("p" * 43), user_id=1,
                                     expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add(AdConnection(id=9, workspace_id=1, project_id=1, platform=avito.ITEMS, name="Авито кухни",
                                    external_account_id="42", access_token_encrypted="x", status="connected"))
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(PORTAL_COOKIE, "p" * 43)
        self.origin = {"Origin": "http://localhost:3000"}

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def fake(self):
        later = int(NOW.timestamp()) + 60
        chats = {"chats": [
            {"id": "c1", "created": later, "updated": later,
             "context": {"type": "item", "value": {"id": 555, "title": "Кухня на заказ", "user_id": 42,
                                                    "url": "https://avito.ru/samara/kuhni_555"}},
             "users": [{"id": 42, "name": "Мы"}, {"id": 7, "name": "Анна"}],
             "last_message": {"direction": "in", "content": {"text": "Сколько стоит?"}}},
            {"id": "old", "created": later - 86400 * 3, "updated": later,
             "context": {"type": "item", "value": {"id": 1, "user_id": 42}}, "users": []},
            {"id": "buyer", "created": later, "updated": later,
             "context": {"type": "item", "value": {"id": 2, "user_id": 999}}, "users": []}]}
        calls = {"calls": [{"id": 31, "buyerPhone": "89990001122", "itemId": 555, "duration": 45,
                            "createTime": (NOW + timedelta(minutes=2)).isoformat()}], "error": {}}
        return FakeClient({
            "/messenger/v2/accounts/42/chats": (200, chats),
            "/messenger/v3/accounts/42/chats/c1/messages/": (200, {"messages": [
                {"direction": "in", "created": later, "content": {"text": "Здравствуйте, нужна кухня 3 м"}}]}),
            "/calltracking/v1/getCalls/": (403, {}),
            "/cpa/v2/callsByTime": (200, calls)})

    def test_enable_import_accept_and_attribute(self):
        enabled = self.client.put("/api/ads/connections/9/avito/leads", headers=self.origin,
                                  json={"chats": True, "calls": True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        fake = self.fake()

        async def client_for(_db, _row):
            return fake
        with patch.object(avito, "client_for", client_for):
            first = self.client.post("/api/ads/connections/9/avito/leads/check", headers=self.origin)
            again = self.client.post("/api/ads/connections/9/avito/leads/check", headers=self.origin)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["imported"], {"chats": 1, "calls": 1})
        self.assertEqual(again.json()["imported"], {"chats": 0, "calls": 0})
        self.assertEqual(first.json()["leads"]["calls_mode"], "cpa")

        async def rows():
            async with self.sessions() as db:
                return (await db.scalars(select(CrmInbound).order_by(CrmInbound.id))).all()
        chat, call = asyncio.run(rows())
        self.assertEqual(chat.raw_payload["item_title"], "Кухня на заказ")
        self.assertEqual(chat.raw_payload["notes"], "Здравствуйте, нужна кухня 3 м")
        self.assertEqual(chat.attribution["verified_connection_id"], 9)
        self.assertEqual(call.phone, "+79990001122")

        accepted = self.client.post(f"/api/crm/inbound/{chat.id}/accept", headers=self.origin, json={})
        self.assertEqual(accepted.status_code, 200, accepted.text)

        async def attribution():
            async with self.sessions() as db:
                return await db.scalar(select(ClientLeadAttribution).where(
                    ClientLeadAttribution.lead_id == accepted.json()["lead_id"]))
        self.assertEqual(asyncio.run(attribution()).connection_id, 9)

    def test_overview_reports_lead_state_and_rejects_ads_routes(self):
        overview = self.client.get("/api/ads/connections/9/avito")
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertFalse(overview.json()["leads"]["chats"])
        self.assertEqual(self.client.get("/api/ads/connections/9/avito/campaigns/5").status_code, 404)


if __name__ == "__main__":
    unittest.main()
