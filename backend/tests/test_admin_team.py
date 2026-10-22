"""Команда агентства: добавить, отключить, сбросить пароль; нельзя отключить себя и последнего админа."""
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

ORIGIN = {"Origin": "http://localhost:3000"}


class TeamTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def init():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="owner", password_hash=hash_password("owner-password-123"), role="admin"))
                await db.commit()
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                await db.commit()
        asyncio.run(init())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI()
        app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.limiter = patch("app.api.routes.access.rate_limit", new=AsyncMock())
        self.limiter.start()
        self.client.cookies.set("stl_session", "a" * 43)

    def tearDown(self):
        self.client.close()
        self.limiter.stop()
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def login(self, username, password):
        other = TestClient(self.client.app)
        r = other.post("/api/auth/login", headers=ORIGIN, json={"username": username, "password": password})
        return other, r

    def test_requires_admin(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/admin/team").status_code, 401)

    def test_add_member_gets_full_admin(self):
        r = self.client.post("/api/admin/team", headers=ORIGIN, json={"username": "Anna@Steptolead.ru", "display_name": "Анна"})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        password = body["password"]
        self.assertGreaterEqual(len(password), 16)
        anna = next(m for m in body["members"] if m["username"] == "anna@steptolead.ru")
        self.assertTrue(anna["active"])
        self.assertEqual(anna["created_by"], "owner")
        self.assertEqual(body["log"][0]["action"], "created")
        other, login = self.login("anna@steptolead.ru", password)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(other.get("/api/campaigns").status_code, 200)  # та же админка, что у владельца
        self.assertEqual(other.get("/api/admin/team").status_code, 200)
        other.close()
        # повтор и кривой логин
        self.assertEqual(self.client.post("/api/admin/team", headers=ORIGIN, json={"username": "anna@steptolead.ru"}).status_code, 409)
        self.assertEqual(self.client.post("/api/admin/team", headers=ORIGIN, json={"username": "аня пётр"}).status_code, 422)

    def test_disable_reset_and_guards(self):
        body = self.client.post("/api/admin/team", headers=ORIGIN, json={"username": "ivan"}).json()
        ivan = next(m for m in body["members"] if m["username"] == "ivan")
        other, _ = self.login("ivan", body["password"])
        self.assertEqual(other.get("/api/auth/me").status_code, 200)
        # отключение закрывает сеансы и вход
        r = self.client.post(f"/api/admin/team/{ivan['id']}/disable", headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(other.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login("ivan", body["password"])[1].status_code, 401)
        # вернуть + сбросить пароль: старый перестаёт работать
        self.client.post(f"/api/admin/team/{ivan['id']}/enable", headers=ORIGIN)
        new = self.client.post(f"/api/admin/team/{ivan['id']}/reset-password", headers=ORIGIN).json()["password"]
        self.assertEqual(self.login("ivan", body["password"])[1].status_code, 401)
        self.assertEqual(self.login("ivan", new)[1].status_code, 200)
        # себя нельзя отключить; последнего активного — тоже
        self.assertEqual(self.client.post("/api/admin/team/1/disable", headers=ORIGIN).status_code, 409)
        self.client.post(f"/api/admin/team/{ivan['id']}/disable", headers=ORIGIN)
        self.assertEqual(self.client.post("/api/admin/team/1/disable", headers=ORIGIN).status_code, 409)
        other.close()

    def test_change_own_password_keeps_current_session(self):
        r = self.client.post("/api/admin/team/me/password", headers=ORIGIN, json={"current": "wrong", "new": "brand-new-password-1"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/admin/team/me/password", headers=ORIGIN,
                             json={"current": "owner-password-123", "new": "brand-new-password-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.login("owner", "owner-password-123")[1].status_code, 401)
        self.assertEqual(self.login("owner", "brand-new-password-1")[1].status_code, 200)


if __name__ == "__main__":
    unittest.main()
