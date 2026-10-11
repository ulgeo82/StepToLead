"""Воронка не падает, когда у проекта есть заявки, но нет квалифицированных (раньше — ошибка 500 в трёх разделах)."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.db import Base
from app.models.marketing import ClientLead, ClientWorkspace, Project
from app.services import result_analytics


class FunnelGapTests(unittest.TestCase):
    def test_leads_without_qualified(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(ClientWorkspace(id=2, name="Агентство"))
                db.add(Project(id=27, workspace_id=2, name="Агентство", is_default=True))
                await db.flush()
                # Заявки пришли в августе (до периода), а квалифицированы в октябре (внутри периода):
                # за период «лидов» нет, а «квалифицированные» есть.
                for i in range(3):
                    db.add(ClientLead(workspace_id=2, project_id=27, full_name=f"Лид {i}", source="Аутрич",
                                      created_at=datetime(2026, 8, 1, 10, i, tzinfo=timezone.utc),
                                      qualified_at=datetime(2026, 10, 1, 10, i, tzinfo=timezone.utc)))
                await db.commit()
                project = await db.get(Project, 27)
                orig = result_analytics._ratio
                try:
                    return await result_analytics.result_facts(db, project, date(2026, 9, 12), date(2026, 10, 11), "day")
                finally:
                    result_analytics._ratio = orig
        try:
            data = asyncio.run(go())
        finally:
            asyncio.run(engine.dispose())
            os.unlink(path)
        funnel = {f["label"]: f for f in data["funnel"]}
        self.assertEqual(funnel["Квалифицированные"]["value"], 3)
        for stage in funnel.values():
            self.assertTrue(stage["conversion"] is None or isinstance(stage["conversion"], float))

    def test_ratio_none_safe(self):
        r = result_analytics._ratio(None, 3)
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
