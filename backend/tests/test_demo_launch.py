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
            lead = CrmInbound(id=5, workspace_id=2, project_id=27, name="Олег", raw_payload={"assessment_id": 3, "launch_request": {"answers": LAUNCH, "at": "2026-10-13T10:00:00+00:00"}})
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
            with patch.object(demo_launch.settings, "agency_workspace_id", 2), patch.object(demo_launch, "check_origin"):
                result = await demo_launch.convert(5, demo_launch.ConvertIn(company_name="Кухни Север"), None, db)
                self.assertEqual(len(await demo_launch.launch_requests(db)), 1)
            ws = await db.get(ClientWorkspace, result["workspace_id"])
            self.assertEqual(ws.name, "Кухни Север")
            # Доступы выдаёт только агентство — пользователей кабинет не получает.
            self.assertIsNone(await db.scalar(select(PortalUser).where(PortalUser.workspace_id == ws.id)))
            pipes = [p.name for p in (await db.scalars(select(CrmPipeline).where(CrmPipeline.project_id == result["project_id"]))).all()]
            self.assertEqual(sorted(pipes), ["Двери", "Кухни на заказ", "Шкафы-купе"])
            brief = await db.scalar(select(ClientBrief).where(ClientBrief.workspace_id == ws.id))
            self.assertEqual(brief.answers["company.geo"], "Самара")
            self.assertEqual((await db.get(ClientWorkspace, 9)).status, "deleted")
            with patch.object(demo_launch.settings, "agency_workspace_id", 2), patch.object(demo_launch, "check_origin"):
                with self.assertRaises(Exception):
                    await demo_launch.convert(5, demo_launch.ConvertIn(company_name="Ещё"), None, db)

    async def test_agency_made_demo_creates_lead(self):
        async with self.sessions() as db:
            demo = await db.get(Project, 90); demo.status = "demo"
            user = PortalUser(workspace_id=9, username="demo9", display_name="Демо", role="client_owner", password_hash="x", active=True)
            db.add(user); await db.commit()
            with patch.object(demo_launch.settings, "agency_workspace_id", 2), patch.object(demo_launch, "check_origin"), \
                    patch("app.services.monitor.send"):
                form = await demo_launch.launch_form(90, db, user)
                self.assertEqual(len(form["questions"]), 8)
                await demo_launch.launch_submit(90, demo_launch.LaunchIn(answers={**LAUNCH, "contact": "+79990001122"}), None, db, user)
                leads = await demo_launch._launch_leads(db)
            new = [l for l in leads if l.id != 5]
            self.assertEqual(len(new), 1)
            self.assertEqual(new[0].raw_payload["demo_workspace_id"], 9)
            self.assertEqual(new[0].status, "NEW")  # в «Неразобранном» агентства

    async def test_go_live_blocks_channels_moves_ads_and_expires(self):
        from datetime import datetime, timedelta, timezone
        from fastapi import HTTPException
        from app.core.crypto import encrypt_secret
        from app.models.marketing import AdConnection
        from app.services import plans
        async with self.sessions() as db:
            demo = await db.get(Project, 90); demo.status = "demo"; demo.portal_state = {"brief_demo": {"assessment_id": 3}}
            user = PortalUser(workspace_id=9, username="demo9", display_name="Демо", role="client_owner", password_hash="x", active=True)
            db.add(user); await db.commit()
            with patch.object(demo_launch, "check_origin"):
                live = await demo_launch.go_live(90, None, db, user)
            project = await db.get(Project, live["project_id"])
            ws = await db.get(ClientWorkspace, project.workspace_id)
            self.assertEqual((project.status, ws.plan, user.workspace_id), ("trial", "trial", ws.id))
            self.assertEqual((await db.get(ClientWorkspace, 9)).status, "deleted")
            self.assertTrue((await db.get(PortalUser, user.id)).active)
            with self.assertRaises(HTTPException):
                await plans.require_full(db, ws.id)  # чаты, почта, телефония, ИИ — нет
            with self.assertRaises(HTTPException):
                await plans.require_seat(db, ws.id)  # второго пользователя не добавить
            db.add(AdConnection(workspace_id=ws.id, project_id=project.id, platform="yandex_direct", name="Директ",
                                external_account_id="client", access_token_encrypted=encrypt_secret("tok"), status="connected"))
            await db.commit()
            with patch.object(demo_launch.settings, "agency_workspace_id", 2), patch.object(demo_launch, "check_origin"):
                result = await demo_launch.convert(5, demo_launch.ConvertIn(company_name="Кухни Север"), None, db)
            self.assertEqual(result["ad_connections"], 1)
            conn = await db.scalar(select(AdConnection))
            self.assertEqual(conn.workspace_id, result["workspace_id"])

    async def test_live_demo_expiry_wipes_tokens(self):
        from datetime import datetime, timedelta, timezone
        from app.core.crypto import encrypt_secret
        from app.models.marketing import AdConnection
        async with self.sessions() as db:
            db.add(ClientWorkspace(id=11, name="Живое демо · X", plan="trial"))
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            db.add(Project(id=111, workspace_id=11, name="X", status="trial", portal_state={"demo_live": {"expires_at": past}}))
            await db.flush()
            db.add(AdConnection(workspace_id=11, project_id=111, platform="yandex_direct", name="Д", external_account_id="c",
                                access_token_encrypted=encrypt_secret("tok"), status="connected"))
            await db.commit()
            self.assertEqual(await demo_launch.expire_live_demos(db), 1)
            conn = await db.scalar(select(AdConnection).where(AdConnection.workspace_id == 11))
            self.assertEqual(conn.access_token_encrypted, "")
            self.assertEqual((await db.get(ClientWorkspace, 11)).status, "deleted")


if __name__ == "__main__":
    unittest.main()
