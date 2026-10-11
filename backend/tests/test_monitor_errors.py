"""Понятные уведомления об ошибках 500: адрес, проект, ошибка и место в коде, повторы сгруппированы."""
import asyncio
import os
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.db import Base
from app.models.marketing import ClientWorkspace, Project
from app.services import monitor


class MonitorErrorTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=2, name="StepToLead (агентство)"))
                db.add(Project(id=27, workspace_id=2, name="Агентство", is_default=True))
                await db.commit()
        asyncio.run(seed())
        monitor._errors.clear()

    def tearDown(self):
        monitor._errors.clear()
        asyncio.run(self.engine.dispose())
        os.unlink(self.path)

    def problems(self):
        async def go():
            async with self.sessions() as db:
                return await monitor.problems(db)
        return asyncio.run(go())

    def test_500_is_reported_with_route_project_and_place(self):
        from app.main import RememberFailures
        app = FastAPI()
        app.add_middleware(RememberFailures)

        @app.get("/api/result")
        async def broken(project_id: int):
            return {"x": {}["sources"]}
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(4):
            self.assertEqual(client.get("/api/result", params={"project_id": 27}).status_code, 500)
        client.get("/api/result", params={"project_id": 27, "other": 1})
        errs = [p for p in self.problems() if p["key"].startswith("err:")]
        self.assertEqual(len(errs), 1)
        p = errs[0]
        self.assertEqual(p["title"], "Ошибка на «GET /api/result» — 5 раз за 15 мин")
        self.assertIn("Ошибка: KeyError: 'sources'", p["detail"])
        self.assertIn("test_monitor_errors.py", p["detail"])  # место в коде, а не в библиотеке
        self.assertIn("Проект: «Агентство» (id 27)", p["detail"])
        self.assertEqual(p["client"], "StepToLead (агентство)")
        line = monitor._line(p)
        self.assertNotIn("Exception in ASGI application", line)

    def test_single_errors_and_background_logs(self):
        import logging
        monitor.install()
        log = logging.getLogger("uvicorn.error.leadgen")
        try:
            raise ValueError("bad xml")
        except ValueError:
            log.exception("tick failed")
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")  # не дублируем
        self.assertEqual(len(monitor._errors), 1)
        self.assertEqual(monitor._errors[0]["route"], "фоновая задача · leadgen")
        self.assertEqual(self.problems(), [])  # одна ошибка — не повод будить


if __name__ == "__main__":
    unittest.main()
