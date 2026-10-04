"""Self-service password reset via the Telegram bot."""
import re
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.routes import portal
from app.models.marketing import PortalSession, PortalUser
from app.services import notifications
import test_stage2

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class ResetTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def test_reset_by_telegram_code(self):
        async def link(db):
            u = await db.get(PortalUser, 2); u.telegram_chat_id = "777"; await db.commit()
        self.run_db(link)
        sent = AsyncMock(return_value={})
        with patch.object(portal, "rate_limit", AsyncMock()), patch.object(notifications, "telegram_api", sent), \
                patch.object(notifications, "telegram_configured", return_value=True):
            unknown = self.client.post("/api/portal/auth/reset/start", headers=ORIGIN, json={"username": "nobody"}).json()
            self.assertEqual(sent.await_count, 0)
            known = self.client.post("/api/portal/auth/reset/start", headers=ORIGIN, json={"username": "U2"}).json()
            self.assertEqual(unknown, known)  # the answer does not reveal whether a login exists
            code = re.search(r"<b>(\d{6})</b>", sent.await_args.args[1]["text"]).group(1)
            self.assertEqual(sent.await_args.args[1]["chat_id"], "777")
            wrong = "000000" if code != "000000" else "111111"
            bad = self.client.post("/api/portal/auth/reset/finish", headers=ORIGIN, json={"username": "u2", "code": wrong, "new_password": "new-password-123"})
            self.assertEqual(bad.status_code, 422)
            ok = self.client.post("/api/portal/auth/reset/finish", headers=ORIGIN, json={"username": "u2", "code": code, "new_password": "new-password-123"})
            self.assertEqual(ok.status_code, 200, ok.text)
            again = self.client.post("/api/portal/auth/reset/finish", headers=ORIGIN, json={"username": "u2", "code": code, "new_password": "other-password-1"})
            self.assertEqual(again.status_code, 422)  # one-time
            self.assertEqual(self.client.post("/api/portal/auth/login", headers=ORIGIN, json={"username": "u2", "password": "new-password-123"}).status_code, 200)
        sessions = self.run_db(lambda db: db.scalars(select(PortalSession).where(PortalSession.user_id == 2))).all()
        self.assertEqual(len(sessions), 1)  # old sessions ended, only the new login

    def test_attempts_run_out(self):
        async def link(db):
            u = await db.get(PortalUser, 2); u.telegram_chat_id = "777"; await db.commit()
        self.run_db(link)
        with patch.object(portal, "rate_limit", AsyncMock()), patch.object(notifications, "telegram_api", AsyncMock(return_value={})), \
                patch.object(notifications, "telegram_configured", return_value=True):
            self.client.post("/api/portal/auth/reset/start", headers=ORIGIN, json={"username": "u2"})
            for _ in range(portal.RESET_ATTEMPTS):
                r = self.client.post("/api/portal/auth/reset/finish", headers=ORIGIN, json={"username": "u2", "code": "999999", "new_password": "new-password-123"})
            self.assertIn("запросите новый", r.json()["detail"])
            self.assertIsNone(self.run_db(lambda db: db.get(PortalUser, 2)).reset_code_hash)


del Base

if __name__ == "__main__":
    unittest.main()
