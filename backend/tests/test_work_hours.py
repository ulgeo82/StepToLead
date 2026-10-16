import asyncio
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.work_hours import business_minutes

SCHEDULE = {"timezone": "Europe/Moscow", "days": [1, 2, 3, 4, 5], "start": "09:00", "end": "19:00"}


class WorkHoursTests(unittest.TestCase):
    def test_friday_to_monday_is_45(self):
        zone = ZoneInfo("Europe/Moscow")
        first = datetime(2026, 10, 16, 18, 30, tzinfo=zone)
        last = datetime(2026, 10, 19, 9, 15, tzinfo=zone)
        self.assertEqual(business_minutes(first, last, SCHEDULE), 45)
        self.assertEqual(business_minutes(first, last, None), 3765)

    def test_utc_naive_and_clipping(self):
        first, last = datetime(2026, 10, 16, 5), datetime(2026, 10, 16, 7)
        self.assertEqual(business_minutes(first, last, SCHEDULE), 60)
        self.assertEqual(business_minutes(last, first, SCHEDULE), 0)
        self.assertEqual(business_minutes(first, first, SCHEDULE), 0)

    def test_no_work_on_weekend_or_empty_days(self):
        self.assertEqual(business_minutes(datetime(2026, 10, 17), datetime(2026, 10, 18), SCHEDULE), 0)
        self.assertEqual(business_minutes(datetime(2026, 10, 16), datetime(2026, 10, 19),
                                          {**SCHEDULE, "days": []}), 0)

    def test_dst_counts_real_elapsed_minutes(self):
        zone = ZoneInfo("Europe/Berlin")
        schedule = {"timezone": "Europe/Berlin", "days": [7], "start": "01:00", "end": "04:00"}
        self.assertEqual(business_minutes(datetime(2026, 3, 29, 1, tzinfo=zone),
                                          datetime(2026, 3, 29, 4, tzinfo=zone), schedule), 120)
        self.assertEqual(business_minutes(datetime(2026, 10, 25, 1, tzinfo=zone),
                                          datetime(2026, 10, 25, 4, tzinfo=zone), schedule), 240)


class WorkScheduleAPITests(unittest.TestCase):
    def setUp(self):
        from test_result import ResultTests
        ResultTests.setUp(self)
        from app.core.access import PORTAL_COOKIE, hash_password, token_digest
        from app.models.marketing import PortalSession, PortalUser, Project
        async def seed():
            async with self.sessions() as db:
                db.add(PortalUser(id=10, workspace_id=1, username="schedule-owner", display_name="Владелец",
                                  role="client_owner", password_hash=hash_password("test-password-123"),
                                  must_change_password=False))
                db.add(PortalSession(token_hash=token_digest("w" * 43), user_id=10,
                                     expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                (await db.get(Project, 1)).portal_state = {"ai": {"knowledge": "Сохранить"}}
                await db.commit()
        asyncio.run(seed())
        self.client.cookies.clear()
        self.client.cookies.set(PORTAL_COOKIE, "w" * 43)

    def tearDown(self):
        from test_result import ResultTests
        ResultTests.tearDown(self)

    def test_save_read_disable_and_preserve_other_settings(self):
        self.assertIsNone(self.client.get("/api/settings/work-schedule?project_id=1").json())
        response = self.client.put("/api/settings/work-schedule?project_id=1", headers=self.origin, json=SCHEDULE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/settings/work-schedule?project_id=1").json(), SCHEDULE)
        from app.models.marketing import Project
        async def check():
            async with self.sessions() as db:
                self.assertEqual((await db.get(Project, 1)).portal_state["ai"], {"knowledge": "Сохранить"})
        asyncio.run(check())
        response = self.client.put("/api/settings/work-schedule?project_id=1", headers=self.origin,
                                   content="null")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json())

    def test_validation_scope_origin_and_permission(self):
        for update in ({"days": [8]}, {"days": [True]}, {"start": "19:00", "end": "09:00"},
                       {"timezone": "No/Such_Zone"}, {"end": "25:00"}):
            response = self.client.put("/api/settings/work-schedule?project_id=1", headers=self.origin,
                                       json={**SCHEDULE, **update})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/settings/work-schedule?project_id=2").status_code, 404)
        self.assertEqual(self.client.put("/api/settings/work-schedule?project_id=1",
                                         headers={"Origin": "https://evil.test"}, json=SCHEDULE).status_code, 403)
        from app.models.marketing import PortalUser
        async def demote():
            async with self.sessions() as db:
                (await db.get(PortalUser, 10)).role = "sales_manager"
                await db.commit()
        asyncio.run(demote())
        self.assertEqual(self.client.get("/api/settings/work-schedule?project_id=1").status_code, 403)
        self.assertEqual(self.client.put("/api/settings/work-schedule?project_id=1", headers=self.origin,
                                         json=SCHEDULE).status_code, 403)

    def test_sales_team_uses_project_schedule(self):
        from datetime import date
        from app.models.crm import CrmContact, CrmDeal, CrmPipeline, CrmStage
        from app.models.marketing import Project
        from app.services.reports import sales_team
        async def run():
            async with self.sessions() as db:
                project = await db.get(Project, 1)
                project.portal_state = {"work_schedule": SCHEDULE}
                db.add(CrmContact(id=1, workspace_id=1, project_id=1, name="Тест"))
                db.add(CrmPipeline(id=1, workspace_id=1, project_id=1, name="Продажи"))
                db.add(CrmStage(id=1, pipeline_id=1, name="Лид", analytics_type="LEAD"))
                db.add(CrmDeal(workspace_id=1, project_id=1, contact_id=1, pipeline_id=1, stage_id=1,
                               name="Тест", responsible_user_id=10,
                               created_at=datetime(2026, 10, 16, 15, 30, tzinfo=timezone.utc),
                               first_response_at=datetime(2026, 10, 19, 6, 15, tzinfo=timezone.utc)))
                await db.commit()
                result = await sales_team(db, project, date(2026, 10, 16), date(2026, 10, 19), set(), set())
                self.assertEqual(result["response_minutes"], 45)
                self.assertEqual(result["managers"][0]["response_minutes"], 45)
        asyncio.run(run())
