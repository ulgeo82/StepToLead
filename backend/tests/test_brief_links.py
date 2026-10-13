"""Бриф по ссылке из сделки: без логина, сделка переезжает на выбранный этап, контакт обновляется."""
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router  # noqa: F401
from app.api.routes import brief_links, crm
from app.db import Base
from app.models.brief import ClientBrief
from app.models.crm import CrmContact, CrmDeal, CrmStage, CrmTask
from app.models.marketing import ClientWorkspace, PortalUser, Project


class BriefLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(ClientWorkspace(id=2, name="Агентство")); db.add(Project(id=27, workspace_id=2, name="Агентство", portal_state={}))
            await db.flush()
            self.user = PortalUser(id=7, workspace_id=2, username="m", display_name="Михаил", role="client_owner", password_hash="x", active=True)
            db.add(self.user)
            pipeline = await crm.pipeline_for(db, 27, commit=False)
            stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id).order_by(CrmStage.position))).all()
            self.brief_stage = CrmStage(pipeline_id=pipeline.id, name="Заполнил бриф", analytics_type="QUALIFIED", position=1, required_fields=[])
            db.add(self.brief_stage); await db.flush()
            contact = CrmContact(workspace_id=2, project_id=27, name="Без имени", phones=[])
            db.add(contact); await db.flush()
            self.deal = CrmDeal(workspace_id=2, project_id=27, contact_id=contact.id, pipeline_id=pipeline.id, stage_id=stages[0].id,
                                name="Кухни Север", responsible_user_id=7)
            db.add(self.deal); await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_flow(self):
        async with self.sessions() as db:
            user = await db.get(PortalUser, 7)
            with patch.object(brief_links, "check_origin"):
                link = await brief_links.make_link(self.deal.id, brief_links.LinkIn(stage_id=self.brief_stage.id), None, db, user)
            token = link["url"].rsplit("/", 1)[1]
            data = await brief_links.read(token, db)
            self.assertTrue(data["sections"])
            body = brief_links.SaveIn(answers={"company.what": "Кухни на заказ"},
                                      contacts={"full_name": "Олег Петров", "phone": "+79990001122", "company": "Кухни Север", "email": "o@sever.ru"})
            with patch("app.api.routes.access.rate_limit", AsyncMock()), patch("app.services.monitor.send", AsyncMock()), \
                    patch("app.services.notifications.flush_telegram"):
                await brief_links.save(token, body, None, db)
                await brief_links.submit(token, body, None, db)
            deal = await db.get(CrmDeal, self.deal.id)
            self.assertEqual(deal.stage_id, self.brief_stage.id)
            contact = await db.get(CrmContact, deal.contact_id)
            self.assertEqual((contact.name, contact.phones[0], contact.company), ("Олег Петров", "+79990001122", "Кухни Север"))
            brief = await db.scalar(select(ClientBrief))
            self.assertEqual((brief.answers["company.what"], brief.status, brief.project_id), ("Кухни на заказ", "review", None))
            self.assertEqual(len((await db.scalars(select(CrmTask))).all()), 1)


if __name__ == "__main__":
    unittest.main()
