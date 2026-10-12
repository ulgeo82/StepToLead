"""Направления из брифа → воронки CRM: безопасно, повторяемо, этапы со сделками не трогаем."""
import json
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router  # noqa: F401
from app.api.routes import crm
from app.db import Base
from app.models.crm import CrmContact, CrmDeal, CrmPipeline, CrmStage
from app.models.marketing import ClientWorkspace, Project
from app.services import brief, brief_funnels


def raw(*directions):
    return json.dumps({"directions": list(directions)}, ensure_ascii=False)


KITCHENS = {"id": "a", "name": "Кухни на заказ", "stages": ["Квалифицирован", "Замер назначен", "Договор"], "qualified_from": 0, "check": "250000"}
WARDROBES = {"id": "b", "name": "Шкафы-купе", "stages": ["Замер", "Договор"], "qualified_from": 1}


class BriefFunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(ClientWorkspace(id=1, name="Кухни"))
            db.add(Project(id=1, workspace_id=1, name="Кухни", portal_state={}))
            await db.flush()
            await crm.pipeline_for(db, 1)  # стандартная воронка: Новый лид, Квалифицирован, Продажа, Отказ

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def stages(self, db, pipeline_id):
        return [(s.name, s.analytics_type) for s in (await db.scalars(select(CrmStage).where(
            CrmStage.pipeline_id == pipeline_id, CrmStage.archived_at.is_(None)).order_by(CrmStage.position))).all()]

    async def test_apply_twice_and_second_direction(self):
        directions = brief_funnels.parse(raw(KITCHENS, WARDROBES))
        async with self.sessions() as db:
            project = await db.get(Project, 1)
            preview = await brief_funnels.plan(db, project, directions)
            self.assertEqual([p["action"] for p in preview], ["update", "create"])
            await brief_funnels.plan(db, project, directions, apply=True); await db.commit()
            pipelines = (await db.scalars(select(CrmPipeline).order_by(CrmPipeline.id))).all()
            self.assertEqual([p.name for p in pipelines], ["Кухни на заказ", "Шкафы-купе"])
            self.assertEqual(await self.stages(db, pipelines[0].id), [("Заявка", "LEAD"), ("Квалифицирован", "QUALIFIED"),
                ("Замер назначен", "QUALIFIED"), ("Договор", "QUALIFIED"), ("Продажа", "WON"), ("Отказ", "LOST")])
            self.assertEqual(await self.stages(db, pipelines[1].id), [("Заявка", "LEAD"), ("Замер", "LEAD"),
                ("Договор", "QUALIFIED"), ("Продажа", "WON"), ("Отказ", "LOST")])
            # Повторный перенос — те же воронки, без дублей.
            again = await brief_funnels.plan(db, project, directions, apply=True); await db.commit()
            self.assertEqual(len((await db.scalars(select(CrmPipeline))).all()), 2)
            self.assertTrue(all(s["status"] == "same" for s in again[0]["stages"]))

    async def test_stage_with_deals_is_kept(self):
        async with self.sessions() as db:
            pipeline = await db.scalar(select(CrmPipeline))
            busy = CrmStage(pipeline_id=pipeline.id, name="Переговоры", analytics_type="QUALIFIED", position=2, required_fields=[])
            db.add(busy); await db.flush()
            contact = CrmContact(workspace_id=1, project_id=1, name="Клиент"); db.add(contact); await db.flush()
            db.add(CrmDeal(workspace_id=1, project_id=1, contact_id=contact.id, pipeline_id=pipeline.id, stage_id=busy.id, name="Сделка"))
            await db.commit()
            project = await db.get(Project, 1)
            report = await brief_funnels.plan(db, project, brief_funnels.parse(raw(KITCHENS)), apply=True); await db.commit()
            self.assertEqual(report[0]["kept"], [{"name": "Переговоры", "deals": 1}])
            names = [n for n, _ in await self.stages(db, pipeline.id)]
            self.assertIn("Переговоры", names)
            self.assertEqual(names[-2:], ["Продажа", "Отказ"])

    def test_parse_and_text(self):
        self.assertEqual(brief_funnels.parse("Заявка → замер → договор"), [])  # старый текст не ломает
        text = brief_funnels.as_text(raw(KITCHENS))
        self.assertIn("Кухни на заказ", text); self.assertIn("Заявка → Квалифицирован", text)
        self.assertIn("Кухни на заказ", brief.markdown({"sales.funnels": raw(KITCHENS)}))


if __name__ == "__main__":
    unittest.main()
