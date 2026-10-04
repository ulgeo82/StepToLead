"""Failure alerts: detection, one alert per problem, «восстановлено», admin screen."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.core.access import COOKIE
from app.models.access import AdminUser
from app.models.marketing import AdConnection
from app.services import monitor
import test_stage2

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class MonitorTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def test_alert_once_then_resolved(self):
        async def broken(db):
            row = await db.get(AdConnection, 7); row.platform = "yandex"; row.status = "error"; row.last_error = "Токен истёк"
            admin = await db.get(AdminUser, 1); admin.telegram_chat_id = "555"
            await db.commit()
        self.run_db(broken)
        send = AsyncMock(return_value=1)
        with patch.object(monitor, "send", send):
            first = self.run_db(monitor.run_once)
            self.assertEqual(first["new"], 1)
            self.assertIn("Ошибка рекламного кабинета", send.await_args.args[1])
            self.assertIn("Токен истёк", send.await_args.args[1])
            self.assertIn("Romax", send.await_args.args[1])
            self.assertEqual(self.run_db(monitor.run_once)["new"], 0)
            self.assertEqual(send.await_count, 1)  # no repeat within 24 hours
            async def fixed(db):
                row = await db.get(AdConnection, 7); row.status = "connected"; await db.commit()
            self.run_db(fixed)
            self.assertEqual(self.run_db(monitor.run_once)["resolved"], 1)
            self.assertIn("Восстановлено", send.await_args.args[1])

    def test_dead_worker_and_error_burst(self):
        async def dead():
            raise RuntimeError("boom")
        loop = asyncio.new_event_loop()
        task = loop.create_task(dead()); loop.run_until_complete(asyncio.sleep(0)); loop.close()
        monitor._tasks.clear(); monitor.register("Телефония", task)
        import logging, time
        monitor.install()
        for _ in range(monitor.ERROR_BURST):
            logging.getLogger("uvicorn.error.crm").error("Exception in ASGI application")
        keys = {p["key"] for p in self.run_db(monitor.problems)}
        self.assertIn("worker:Телефония", keys)
        self.assertIn("errors", keys)
        monitor._tasks.clear(); monitor._errors.clear()
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        overview = self.client.get("/api/admin/monitor").json()
        self.assertEqual(overview["telegram"]["linked"], False)
        self.as_user(1)
        self.assertEqual(self.client.get("/api/admin/monitor").status_code, 401)


del Base

if __name__ == "__main__":
    unittest.main()
