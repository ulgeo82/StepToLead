"""Канал из входящего источника не задваивается, а каналы одного вида получают общую группу."""
import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401
from app.db import Base
from app.models.marketing import (ClientLead, ClientLeadAttribution, ClientWorkspace, LeadInboundSource,
                                  Project, ProjectSource)
from app.services import result_analytics


class ChannelGroupTests(unittest.TestCase):
    def test_mirrored_inbound_counted_once_and_grouped(self):
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
                ids = []
                for i, name in enumerate(["Telegram-профиль: Профиль", "Telegram-профиль: Михаил Фёдоров"]):
                    src = LeadInboundSource(workspace_id=2, project_id=27, name=name, token_hash=f"h{i}", token_prefix="p")
                    db.add(src); await db.flush()
                    ps = ProjectSource(project_id=27, name=name, kind="INTEGRATION", method="API", inbound_source_id=src.id)
                    db.add(ps); await db.flush()
                    ids.append((src.id, ps.id))
                lead = ClientLead(workspace_id=2, project_id=27, full_name="Лид", source="Telegram-профиль: Профиль",
                                  created_at=datetime(2026, 10, 5, tzinfo=timezone.utc))
                db.add(lead); await db.flush()
                db.add(ClientLeadAttribution(lead_id=lead.id, source_id=ids[0][0]))
                await db.commit()
                return await result_analytics.result_facts(db, await db.get(Project, 27),
                                                           date(2026, 10, 1), date(2026, 10, 11), "day")
        try:
            data = asyncio.run(go())
        finally:
            asyncio.run(engine.dispose())
            os.unlink(path)
        sources = data["current"]["sources"]
        names = [s["name"] for s in sources]
        self.assertEqual(names.count("Telegram-профиль: Профиль"), 1, names)
        self.assertEqual(names.count("Telegram-профиль: Михаил Фёдоров"), 1, names)
        profile = next(s for s in sources if s["name"] == "Telegram-профиль: Профиль")
        self.assertEqual(profile["leads"], 1)
        self.assertEqual(profile["group"], "Telegram-профиль")

    def test_group_name(self):
        self.assertEqual(result_analytics.channel_group("Tilda: StepToLead(main)"), "Tilda")
        self.assertIsNone(result_analytics.channel_group("Яндекс Директ"))
        self.assertIsNone(result_analytics.channel_group("Не определено"))


if __name__ == "__main__":
    unittest.main()
