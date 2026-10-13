"""Из демо в клиента: ответы переносятся в бриф, воронки по направлениям, новый чистый кабинет, демо удаляется."""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router  # noqa: F401
from app.api.routes import demo_launch
from app.db import Base
from app.models.brief import ClientBrief, ExpressAssessment
from app.models.crm import CrmInbound, CrmPipeline
from app.models.marketing import ClientWorkspace, PortalUser, Project

EXPRESS = {"what": "Кухни на заказ", "city": "Самара", "company": "Кухни Север", "check": "250000", "leads": "40",
           "cpl": "1500", "conv": "2", "budget": "50–150 тыс. ₽", "speed": "До 15 минут", "goal": "60 заявок в месяц",
           "start": "В этом месяце", "name": "Олег", "contact": "@oleg_kitchen", "channels": "Авито, Яндекс Директ"}
LAUNCH = {"directions": "Кухни на заказ\nШкафы-купе\n- Двери", "crm": "Таблица", "team": "2–3 человека", "site": "sever.ru",
          "budget": "50–150 тыс. ₽", "start": "В этом месяце", "call": "Завтра", "contact": "@oleg_kitchen"}


class DemoLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([ClientWorkspace(id=2, name="Агентство"), ClientWorkspace(id=9, name="Демо · Кухни · 9")])
            db.add_all([Project(id=27, workspace_id=2, name="Агентство"), Project(id=90, workspace_id=9, name="Кухни", portal_state={})])
            await db.flush()
            lead = CrmInbound(id=5, workspace_id=2, project_id=27, name="Олег", raw_payload={"launch_request": {"answers": LAUNCH, "at": "2026-10-13T10:00:00+00:00"}})
            db.add(lead); await db.flush()
            db.add(ExpressAssessment(id=3, lead_id=5, answers=EXPRESS, ip_hash="x", demo_workspace_id=9))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_mapping(self):
        answers = demo_launch.brief_answers(EXPRESS, LAUNCH)
        self.assertEqual(answers["company.name"], "Кухни Север")
        self.assertEqual(answers["sales.conv"], "20")
        names = [d["name"] for d in json.loads(answers["sales.funnels"])["directions"]]
        self.assertEqual(names, ["Кухни на заказ", "Шкафы-купе", "Двери"])
        first = json.loads(answers["sales.funnels"])["directions"][0]
        self.assertIn("Замер назначен", first["stages"])  # шаблон «кухни» подобран по названию

    async def test_convert_creates_clean_workspace(self):
        async with self.sessions() as db:
            with patch.object(demo_launch, "check_origin"):
                result = await demo_launch.convert(3, demo_launch.ConvertIn(company_name="Кухни Север", owner_name="Олег Петров",
                                                                            owner_username="oleg"), None, db)
            self.assertTrue(result["password"])
            ws = await db.get(ClientWorkspace, result["workspace_id"])
            self.assertEqual(ws.name, "Кухни Север")
            owner = await db.scalar(select(PortalUser).where(PortalUser.username == "oleg"))
            self.assertEqual((owner.workspace_id, owner.role), (ws.id, "client_owner"))
            pipes = [p.name for p in (await db.scalars(select(CrmPipeline).where(CrmPipeline.project_id == result["project_id"]))).all()]
            self.assertEqual(sorted(pipes), ["Двери", "Кухни на заказ", "Шкафы-купе"])
            brief = await db.scalar(select(ClientBrief).where(ClientBrief.workspace_id == ws.id))
            self.assertEqual(brief.answers["company.geo"], "Самара")
            self.assertEqual((await db.get(ClientWorkspace, 9)).status, "deleted")
            self.assertEqual(len(await demo_launch.launch_requests(db)), 1)
            with patch.object(demo_launch, "check_origin"):
                with self.assertRaises(Exception):
                    await demo_launch.convert(3, demo_launch.ConvertIn(company_name="Ещё", owner_name="Олег", owner_username="oleg2"), None, db)


if __name__ == "__main__":
    unittest.main()
