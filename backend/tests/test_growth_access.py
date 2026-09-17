"""Изолированная SQLite: не запускает runner и не трогает рабочую базу."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser, GrowthCalculation


class AccessTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async def initialize():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add_all([AdminUser(id=1, username="test-admin", password_hash=hash_password("test-password-123"), role="admin"), AdminUser(id=2, username="viewer", password_hash=hash_password("test-password-123"), role="viewer")])
                await db.commit()
                for token, user_id, hours in [("a" * 43, 1, 1), ("v" * 43, 2, 1), ("e" * 43, 1, -1)]:
                    db.add(AdminSession(token_hash=token_digest(token), user_id=user_id, expires_at=datetime.now(timezone.utc) + timedelta(hours=hours)))
                await db.commit()
        asyncio.run(initialize())
        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI()
        app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.origin = {"Origin": "http://localhost:3000"}
        self.limiter = patch("app.api.routes.access.rate_limit", new=AsyncMock())
        self.limiter.start()
        self.lead_limiter = patch("app.api.routes.growth.rate_limit", new=AsyncMock())
        self.lead_limiter.start()

    def tearDown(self):
        self.client.close()
        self.limiter.stop(); self.lead_limiter.stop()
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def auth(self, token="a" * 43):
        self.client.cookies.set("stl_session", token)

    def test_public_calculator_without_login(self):
        response = self.client.post("/api/public/growth/calculate", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["safetyMargin"], .375)

    def test_all_internal_get_endpoints_require_admin(self):
        for path in ["/campaigns", "/leads", "/telegram-accounts", "/telegram-accounts/config", "/proxies", "/admin/growth/leads", "/auth/me"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/api" + path).status_code, 401)

    def test_invalid_expired_and_nonadmin(self):
        for token, code in [("fake" * 11, 401), ("e" * 43, 401), ("v" * 43, 403)]:
            self.auth(token)
            self.assertEqual(self.client.get("/api/campaigns").status_code, code)

    def test_admin_can_read_old_modules(self):
        self.auth()
        for path in ["/campaigns", "/leads", "/telegram-accounts", "/proxies", "/admin/growth/leads"]:
            self.assertEqual(self.client.get("/api" + path).status_code, 200, path)

    def test_login_cookie_and_logout_revoke(self):
        response = self.client.post("/api/auth/login", headers=self.origin, json={"username": "test-admin", "password": "test-password-123"})
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=strict", cookie)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        token = self.client.cookies.get("stl_session")
        self.assertEqual(self.client.post("/api/auth/logout", headers=self.origin).status_code, 200)
        self.auth(token)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_csrf_and_invalid_password(self):
        payload = {"username": "test-admin", "password": "wrong"}
        self.assertEqual(self.client.post("/api/auth/login", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", json=payload, headers=self.origin).status_code, 401)
        self.auth()
        self.assertEqual(self.client.post("/api/campaigns", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_websocket_denies_unauthorized(self):
        for token in ["", "fake" * 11, "v" * 43, "e" * 43]:
            self.client.cookies.clear()
            if token: self.auth(token)
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("/api/telegram-accounts/ws/status", headers=self.origin):
                    pass

    def test_save_contact_one_channel_recalculates(self):
        response = self.client.post("/api/public/growth/leads", json={"inputs": {}, "name": "Тест", "telegram": "@tester", "consent": True})
        self.assertEqual(response.status_code, 201)
        self.auth()
        row = self.client.get("/api/admin/growth/leads").json()[0]
        self.assertEqual(row["results"]["metrics"]["currentOperatingProfit"], 300000)
        self.assertEqual(row["status"], "TEST_READY")
        self.assertEqual(self.client.get("/api/leads").json(), [])
        self.assertEqual(self.client.get("/api/public/growth/leads").status_code, 405)

    def test_contact_validation_and_cannot_inject_results(self):
        for changes in [{}, {"phone": "123"}, {"telegram": "x"}, {"email": "bad"}, {"email": "a@b.ru", "consent": False}, {"email": "a@b.ru", "results": {"status": "TEST_READY"}}]:
            payload = {"inputs": {}, "name": "Тест", "consent": True, **changes}
            self.assertEqual(self.client.post("/api/public/growth/leads", json=payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
