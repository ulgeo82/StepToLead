"""CRM 2.0: automations, bulk actions, contacts merge, task chaining, pipelines, report and export."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import PORTAL_COOKIE, hash_password, token_digest
from app.db import Base, get_db
from app.models.crm import CrmContact, CrmDeal, CrmTask
from app.models.marketing import ClientWorkspace, PortalNotification, PortalSession, PortalUser, Project
from app.services import crm_automation

ORIGIN = {"Origin": "http://localhost:3000"}


class CrmProTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(ClientWorkspace(id=1, name="One"))
                db.add(Project(id=1, workspace_id=1, name="First", is_default=True))
                db.add(PortalUser(id=1, workspace_id=1, username="owner", display_name="Owner",
                                  password_hash=hash_password("test-password-123"), role="client_owner",
                                  must_change_password=False))
                db.add(PortalSession(token_hash=token_digest("p" * 43), user_id=1,
                                     expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.client.cookies.set(PORTAL_COOKIE, "p" * 43)
        pipelines = self.client.get("/api/crm/projects/1/pipelines")
        self.assertEqual(pipelines.status_code, 200, pipelines.text)
        self.pipeline = pipelines.json()[0]
        self.stage = {s["analytics_type"]: s["id"] for s in self.pipeline["stages"]}

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def deal(self, name="Кухня", phone="+79990000001", **extra):
        response = self.client.post("/api/crm/deals", headers=ORIGIN, json={
            "project_id": 1, "name": name, "contact_name": f"Клиент {name}", "phone": phone, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def query(self, fn):
        async def run():
            async with self.sessions() as db:
                return await fn(db)
        return asyncio.run(run())

    def test_recommended_rules_fire_on_create_and_stage_enter(self):
        installed = self.client.post(f"/api/crm/projects/1/automations/recommended?pipeline_id={self.pipeline['id']}",
                                     headers=ORIGIN, json={})
        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertGreaterEqual(installed.json()["created"], 4)
        again = self.client.post(f"/api/crm/projects/1/automations/recommended?pipeline_id={self.pipeline['id']}",
                                 headers=ORIGIN, json={})
        self.assertEqual(again.json()["created"], 0)  # idempotent
        deal_id = self.deal()
        detail = self.client.get(f"/api/crm/deals/{deal_id}").json()
        self.assertEqual([t["title"] for t in detail["tasks"]], ["Связаться с новым клиентом"])
        self.assertEqual(detail["tasks"][0]["priority"], "HIGH")
        self.assertTrue(any(a["event_type"] == "AUTOMATION" for a in detail["activities"]))
        moved = self.client.post(f"/api/crm/deals/{deal_id}/move", headers=ORIGIN, json={"stage_id": self.stage["QUALIFIED"]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertIn("Создана задача «Подготовить и отправить КП»", moved.json()["automations"])
        detail = self.client.get(f"/api/crm/deals/{deal_id}").json()
        self.assertIsNotNone(detail["first_response_at"])  # moving the deal is the first human touch
        self.assertEqual(detail["days_in_stage"], 0)

    def test_idle_rule_fires_once_per_stage_entry(self):
        rule = self.client.post("/api/crm/projects/1/automations", headers=ORIGIN, json={
            "pipeline_id": self.pipeline["id"], "stage_id": self.stage["LEAD"], "name": "Сутки без движения",
            "trigger": "NO_ACTIVITY", "delay_minutes": 1440, "action": "NOTIFY",
            "params": {"to": "owner", "text": "Лид без движения"}})
        self.assertEqual(rule.status_code, 201, rule.text)
        deal_id = self.deal()

        async def age(db):
            deal = await db.get(CrmDeal, deal_id)
            deal.last_activity_at = datetime.now(timezone.utc) - timedelta(days=2)
            await db.commit()
        self.query(age)
        first = self.query(lambda db: crm_automation.run_idle_rules(db, 1))
        second = self.query(lambda db: crm_automation.run_idle_rules(db, 1))
        self.assertEqual((first, second), (1, 0))
        notes = self.query(lambda db: db.scalars(select(PortalNotification.title)))
        self.assertIn("Лид без движения", list(notes))
        listed = self.client.get("/api/crm/projects/1/automations").json()
        self.assertEqual(listed[0]["fired_count"], 1)

    def test_bulk_tags_responsible_and_guarded_moves(self):
        a, b = self.deal("A", "+79990000011"), self.deal("B", "+79990000012")
        tagged = self.client.post("/api/crm/deals/bulk", headers=ORIGIN, json={"deal_ids": [a, b], "action": "tag_add", "tag": " горячий "})
        self.assertEqual(tagged.json()["updated"], 2)
        board = self.client.get("/api/crm/projects/1/board?tag=горячий").json()
        self.assertEqual(sum(c["total"] for c in board["columns"]), 2)
        won = self.client.post("/api/crm/deals/bulk", headers=ORIGIN, json={"deal_ids": [a], "action": "move", "stage_id": self.stage["WON"]})
        self.assertEqual(won.json()["skipped"], [a])  # a sale needs an amount and confirmation in the card
        moved = self.client.post("/api/crm/deals/bulk", headers=ORIGIN, json={"deal_ids": [a, b], "action": "move", "stage_id": self.stage["QUALIFIED"]})
        self.assertEqual(moved.json()["updated"], 2)
        board = self.client.get(f"/api/crm/projects/1/board?search=Клиент A").json()
        self.assertEqual(sum(c["total"] for c in board["columns"]), 1)
        self.assertIn("amount", board["columns"][0])

    def test_contact_card_merge_and_edit(self):
        first, second = self.deal("A", "+7 999 000-00-21"), self.deal("B", "89990000021")
        contact_a = self.client.get(f"/api/crm/deals/{first}").json()["contact"]["id"]
        contact_b = self.client.get(f"/api/crm/deals/{second}").json()["contact"]["id"]
        card = self.client.get(f"/api/crm/contacts/{contact_a}").json()
        self.assertEqual([d["id"] for d in card["duplicates"]], [contact_b])
        merged = self.client.post(f"/api/crm/contacts/{contact_a}/merge", headers=ORIGIN, json={"source_contact_id": contact_b})
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(merged.json()["moved_deals"], 1)
        card = self.client.get(f"/api/crm/contacts/{contact_a}").json()
        self.assertEqual(len(card["deals"]), 2)
        self.assertIsNone(self.query(lambda db: db.get(CrmContact, contact_b)))
        edited = self.client.patch(f"/api/crm/contacts/{contact_a}", headers=ORIGIN, json={
            "name": "Анна Смирнова", "company": "ООО Ромашка", "tags": ["VIP", "vip"], "emails": ["Anna@Example.ru"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        card = self.client.get(f"/api/crm/contacts/{contact_a}").json()
        self.assertEqual((card["name"], card["tags"], card["emails"]), ("Анна Смирнова", ["VIP"], ["anna@example.ru"]))

    def test_complete_task_with_next_step_and_touch(self):
        deal_id = self.deal()
        task = self.client.post("/api/crm/tasks", headers=ORIGIN, json={"project_id": 1, "deal_id": deal_id, "type_code": "CALL",
            "title": "Позвонить", "due_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}).json()
        done = self.client.post(f"/api/crm/tasks/{task['id']}/complete", headers=ORIGIN, json={"result": "Не дозвонился",
            "next_task": {"type_code": "CALL", "title": "Перезвонить", "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["next_task"]["title"], "Перезвонить")
        open_tasks = self.query(lambda db: db.scalars(select(CrmTask.title).where(CrmTask.status == "OPEN")))
        self.assertEqual(list(open_tasks), ["Перезвонить"])
        touch = self.client.post(f"/api/crm/deals/{deal_id}/touch", headers=ORIGIN, json={"kind": "CALL", "text": "Договорились о замере"})
        self.assertEqual(touch.status_code, 200, touch.text)
        events = [a["event_type"] for a in self.client.get(f"/api/crm/deals/{deal_id}").json()["activities"]]
        self.assertIn("CALL_LOGGED", events)

    def test_pipelines_report_and_export(self):
        copy = self.client.post("/api/crm/projects/1/pipelines", headers=ORIGIN, json={"name": "Повторные продажи", "copy_from_pipeline_id": self.pipeline["id"]})
        self.assertEqual(copy.status_code, 201, copy.text)
        pipelines = self.client.get("/api/crm/projects/1/pipelines").json()
        self.assertEqual(len(pipelines), 2)
        self.assertEqual(len(pipelines[1]["stages"]), len(self.pipeline["stages"]))
        refused = self.client.patch(f"/api/crm/pipelines/{self.pipeline['id']}", headers=ORIGIN, json={"archived": True})
        self.assertEqual(refused.status_code, 409)
        a, b = self.deal("A", "+79990000031", amount=100000), self.deal("B", "+79990000032")
        self.client.post(f"/api/crm/deals/{a}/move", headers=ORIGIN, json={"stage_id": self.stage["QUALIFIED"]})
        won = self.client.post(f"/api/crm/deals/{a}/move", headers=ORIGIN, json={"stage_id": self.stage["WON"]})
        self.assertTrue(won.json()["sale_required"])
        self.client.post(f"/api/crm/deals/{a}/sales", headers=ORIGIN, json={"amount": 120000})
        report = self.client.get(f"/api/crm/projects/1/report?pipeline_id={self.pipeline['id']}")
        self.assertEqual(report.status_code, 200, report.text)
        data = report.json()
        self.assertEqual((data["totals"]["deals"], data["totals"]["won"], data["totals"]["revenue"]), (2, 1, 120000))
        self.assertEqual([s["count"] for s in data["funnel"]], [2, 1, 1])
        self.assertEqual(data["managers"][0]["name"], "Owner")
        csv = self.client.get("/api/crm/projects/1/deals.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Сделка;Воронка;Этап", csv.text)
        self.assertIn("Клиент A", csv.text)
        _ = b


if __name__ == "__main__":
    unittest.main()
