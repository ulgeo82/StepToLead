"""Telephony (Mango Office): connection, signed webhooks, call log, missed-call tasks, click-to-call, recordings."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import PORTAL_COOKIE, hash_password, token_digest
from app.core.config import settings
from app.db import Base, get_db
from app.models.crm import CrmInbound, CrmTask
from app.models.marketing import ClientWorkspace, PortalProjectAccess, PortalSession, PortalUser, Project
from app.models.telephony import Call
from app.services import telephony
from app.services.telephony import Mango

ORIGIN = {"Origin": "http://localhost:3000"}
KEY, SALT = "vpbx-key-123", "salt-456"
PBX_USERS = [{"extension": "101", "name": "User 2", "numbers": []}, {"extension": "102", "name": "Оператор", "numbers": []}]
T0 = int(datetime.now(timezone.utc).timestamp()) - 600


def summary(entry, direction=1, phone="89001234567", extension="101", talk=0, end_offset=40, result=None):
    src, dst = ({"number": phone}, {"extension": extension, "number": "sip:101@x"}) if direction == 1 else \
        ({"extension": extension, "number": "74950000000"}, {"number": phone})
    return {"entry_id": entry, "call_direction": direction, "from": src, "to": dst, "line_number": "74951112233",
            "create_time": T0, "forward_time": T0 + 2, "talk_time": T0 + talk if talk else 0, "end_time": T0 + end_offset,
            "entry_result": result if result is not None else (1 if talk else 0), "disconnect_reason": 1110}


class TelephonyTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.records = tempfile.TemporaryDirectory()
        telephony._recent_dials.clear()
        self.old_dir, settings.recordings_dir = settings.recordings_dir, self.records.name
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=1, name="One"))
                db.add(Project(id=1, workspace_id=1, name="First", is_default=True))
                for uid, role in ((1, "client_owner"), (2, "sales_manager"), (3, "sales_manager")):
                    db.add(PortalUser(id=uid, workspace_id=1, username=f"u{uid}", display_name=f"User {uid}",
                                      password_hash=hash_password("test-password-123"), role=role, must_change_password=False))
                    db.add(PortalSession(token_hash=token_digest(str(uid) * 43), user_id=uid,
                                         expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.flush()
                db.add_all([PortalProjectAccess(user_id=2, project_id=1), PortalProjectAccess(user_id=3, project_id=1)])
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
        settings.recordings_dir = self.old_dir
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path); self.records.cleanup()

    def as_user(self, uid):
        self.client.cookies.clear(); self.client.cookies.set(PORTAL_COOKIE, str(uid) * 43)

    def read(self, fn):
        async def run():
            async with self.sessions() as db:
                return await fn(db)
        return asyncio.run(run())

    def connect(self):
        with patch.object(Mango, "users", AsyncMock(return_value=PBX_USERS)):
            created = self.client.post("/api/crm/projects/1/telephony", headers=ORIGIN,
                                       json={"provider": "mango", "api_key": KEY, "api_salt": SALT})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def event(self, conn, kind, data, salt=SALT):
        form = Mango(KEY, salt).form(data)
        return self.client.post(f"{conn['webhook_path']}/events/{kind}", content=urlencode(form),
                                headers={"Content-Type": "application/x-www-form-urlencoded"})

    def test_connect_maps_extensions_and_hides_secrets(self):
        conn = self.connect()
        self.assertEqual(conn["user_map"], {"101": 2})
        self.assertTrue(conn["webhook_path"].startswith("/api/telephony/mango/"))
        self.as_user(2)
        mine = self.client.get("/api/crm/projects/1/telephony").json()
        self.assertEqual(mine["my_extension"], "101")
        self.assertNotIn("webhook_path", mine["connection"])
        self.assertEqual(self.client.post("/api/crm/projects/1/telephony", headers=ORIGIN,
                                          json={"api_key": KEY, "api_salt": SALT}).status_code, 403)
        self.as_user(1)
        bad = self.client.patch(f"/api/crm/telephony/{conn['id']}", headers=ORIGIN, json={"user_map": {"101": 2, "102": 2}})
        self.assertEqual(bad.status_code, 422)
        ok = self.client.patch(f"/api/crm/telephony/{conn['id']}", headers=ORIGIN, json={"user_map": {"102": 3, "101": 2}})
        self.assertEqual(ok.json()["user_map"], {"102": 3, "101": 2})

    def test_signature_is_required(self):
        conn = self.connect()
        self.assertEqual(self.event(conn, "summary", summary("e1"), salt="wrong").status_code, 403)
        self.assertEqual(self.read(lambda db: db.scalar(select(Call.id))), None)
        self.assertEqual(self.event(conn, "ping", {}).status_code, 200)

    def test_missed_call_creates_request_and_callback_task_then_closes_it(self):
        conn = self.connect()
        self.assertEqual(self.event(conn, "summary", summary("e1")).status_code, 200)
        self.assertEqual(self.event(conn, "summary", summary("e1")).status_code, 200)  # Mango retry: no duplicates
        self.event(conn, "summary", summary("e2", phone="+7 (900) 123-45-67"))
        inbound = self.read(lambda db: db.scalars(select(CrmInbound))).all()
        self.assertEqual(len(inbound), 1)
        self.assertEqual(inbound[0].phone, "+79001234567")
        tasks = self.read(lambda db: db.scalars(select(CrmTask))).all()
        self.assertEqual([(t.title, t.priority, t.status) for t in tasks],
                         [("Перезвонить +7 900 123-45-67", "HIGH", "OPEN")])
        self.assertEqual(tasks[0].deal_id, inbound[0].deal_id)
        log = self.client.get("/api/crm/projects/1/calls").json()
        self.assertEqual(log["stats"]["total"]["missed_in"], 2)
        self.assertEqual(log["stats"]["open_callbacks"], 1)
        self.assertIn("OPEN", [c["callback_status"] for c in log["items"]])
        # Manager calls back and reaches the client: the task closes itself.
        self.event(conn, "summary", summary("e3", direction=2, talk=10, end_offset=130))
        task = self.read(lambda db: db.scalar(select(CrmTask)))
        self.assertEqual(task.status, "COMPLETED")
        # Calls are a trusted source: the first call already became a deal with the whole call history.
        self.assertEqual(inbound[0].status, "ACCEPTED")
        deal_id = inbound[0].deal_id
        calls = self.client.get(f"/api/crm/deals/{deal_id}/calls").json()
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(c["duration_sec"] for c in calls), [0, 0, 120])
        events = [a for a in self.client.get(f"/api/crm/deals/{deal_id}").json()["activities"] if a["event_type"] == "CALL_LOGGED"]
        self.assertEqual(len(events), 3)
        # The next missed call from a known client goes straight to the deal owner.
        self.event(conn, "summary", summary("e4"))
        new_task = self.read(lambda db: db.scalar(select(CrmTask).where(CrmTask.status == "OPEN")))
        self.assertEqual(new_task.deal_id, deal_id)

    def test_incoming_popup_and_visibility(self):
        conn = self.connect()
        live = {"entry_id": "e9", "call_id": "c1", "timestamp": T0 + 600, "call_state": "Appeared", "location": "abonent",
                "from": {"number": "79005556677"}, "to": {"extension": "101", "line_number": "74951112233"}}
        self.assertEqual(self.event(conn, "call", live).status_code, 200)
        self.as_user(2)
        popup = self.client.get("/api/crm/projects/1/calls/active").json()
        self.assertEqual([(c["phone"], c["user_id"]) for c in popup], [("+79005556677", 2)])
        self.as_user(3)
        self.assertEqual(self.client.get("/api/crm/projects/1/calls/active").json(), [])
        self.as_user(1)
        self.event(conn, "summary", summary("e9", phone="79005556677", talk=5, end_offset=65))
        self.as_user(2)
        self.assertEqual(self.client.get("/api/crm/projects/1/calls/active").json(), [])
        self.assertEqual(len(self.client.get("/api/crm/projects/1/calls").json()["items"]), 1)
        self.as_user(3)
        self.assertEqual(self.client.get("/api/crm/projects/1/calls").json()["items"], [])

    def test_click_to_call(self):
        self.connect()
        self.as_user(3)
        self.assertEqual(self.client.post("/api/crm/projects/1/calls/dial", headers=ORIGIN, json={"phone": "89001234567"}).status_code, 422)
        self.as_user(2)
        with patch.object(Mango, "callback", AsyncMock()) as callback:
            response = self.client.post("/api/crm/projects/1/calls/dial", headers=ORIGIN, json={"phone": "8 (900) 123-45-67"})
        self.assertEqual(response.status_code, 200, response.text)
        callback.assert_awaited_once_with("101", "79001234567")

    def test_recording_download_and_retention(self):
        conn = self.connect()
        self.event(conn, "summary", summary("e5", talk=3, end_offset=33))
        self.event(conn, "recording", {"entry_id": "e5", "recording_id": "rec-1", "recording_state": "Completed"})
        call_id = self.read(lambda db: db.scalar(select(Call.id)))
        with patch.object(Mango, "recording", AsyncMock(return_value=b"ID3" + b"0" * 500)):
            audio = self.client.get(f"/api/crm/calls/{call_id}/recording")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/mpeg")
        stored = Path(self.records.name) / "1" / f"{call_id}.mp3"
        self.assertTrue(stored.is_file())

        async def age(db):
            call = await db.get(Call, call_id)
            call.started_at = datetime.now(timezone.utc) - timedelta(days=91)
            await db.commit()
            return await telephony.purge_expired(db)
        self.assertEqual(self.read(age), 1)
        self.assertFalse(stored.exists())
        self.assertEqual(self.client.get(f"/api/crm/calls/{call_id}/recording").status_code, 410)


if __name__ == "__main__":
    unittest.main()
