"""Project boundaries and real-data arithmetic for /result."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import hash_password, token_digest
from app.db import Base, get_db
from app.models.access import AdminSession, AdminUser, GrowthCalculation
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdMetricDaily,
                                  ClientLead, ClientLeadAttribution, ClientSale, ClientWorkspace,
                                  Project, ProjectEconomics, ProjectSource, SourceMetricDaily)


class ResultTests(unittest.TestCase):
    def setUp(self):
        async def no_rate_limit(*_args, **_kwargs):
            return None
        self.rate_limit_patch = patch("app.api.routes.portal.rate_limit", no_rate_limit)
        self.rate_limit_patch.start()
        fd, self.path = tempfile.mkstemp(suffix=".sqlite"); os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def seed():
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with self.sessions() as db:
                db.add(AdminUser(id=1, username="admin", role="admin", password_hash=hash_password("test-password-123")))
                db.add(AdminSession(token_hash=token_digest("a" * 43), user_id=1,
                                    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                db.add_all([ClientWorkspace(id=1, name="One"), ClientWorkspace(id=2, name="Two")])
                db.add_all([Project(id=1, workspace_id=1, name="First", is_default=True),
                            Project(id=2, workspace_id=2, name="Second", is_default=True)])
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.origin = {"Origin": "http://localhost:3000"}
        self.client.cookies.set("stl_session", "a" * 43)

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)
        self.rate_limit_patch.stop()

    def test_vk_campaign_settings_are_read_only_and_project_scoped(self):
        async def seed_connections():
            async with self.sessions() as db:
                db.add_all([
                    AdConnection(id=71, workspace_id=1, project_id=1, platform="vk_ads", name="VK One",
                                 external_account_id="1", access_token_encrypted="unused", status="connected"),
                    AdConnection(id=72, workspace_id=2, project_id=2, platform="vk_ads", name="VK Two",
                                 external_account_id="2", access_token_encrypted="unused", status="connected"),
                ])
                await db.commit()
        asyncio.run(seed_connections())
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "vk-owner", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "vk-owner", "password": owner["temporary_password"]})
        with patch("app.api.routes.ads.verified_vk_token", new=AsyncMock(return_value="test-token")), \
             patch("app.api.routes.marketing._vk_campaigns", return_value=[{"id": "42", "name": "Test"}]):
            own = self.client.get("/api/ads/connections/71/vk-campaigns")
            foreign = self.client.get("/api/ads/connections/72/vk-campaigns")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["campaigns"][0]["id"], "42")
        self.assertEqual(foreign.status_code, 404)

    def test_settings_are_project_scoped_and_drive_lead_workflow(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "settings-owner", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "settings-owner", "password": owner["temporary_password"]})
        self.assertEqual(self.client.get("/api/settings?project_id=2").status_code, 404)
        initial = self.client.get("/api/settings?project_id=1")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json()["company"]["name"], "One")
        self.assertIn("Не подходит по бюджету", [r["label"] for r in initial.json()["reasons"]])
        self.assertEqual(self.client.patch("/api/settings/company?project_id=1", headers=self.origin,
            json={"name": "", "currency": "RUB"}).status_code, 422)
        self.assertEqual(self.client.patch("/api/settings/company?project_id=1", headers=self.origin,
            json={"legal_name": "ООО Тест", "currency": "EUR"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings?project_id=1").json()["company"]["currency"], "EUR")
        self.assertEqual(self.client.patch("/api/settings/funnel?project_id=1", headers=self.origin,
            json={"meeting_enabled": True}).status_code, 200)
        self.assertTrue(self.client.get("/api/result?project_id=1").json()["project"]["meeting_enabled"])
        source = self.client.post("/api/settings/sources?project_id=1", headers=self.origin,
            json={"name": "Партнёры", "kind": "MANUAL", "method": "MANUAL"})
        self.assertEqual(source.status_code, 201, source.text)
        source_id = source.json()["id"].split(":")[1]
        self.assertEqual(self.client.patch(f"/api/settings/sources/{source_id}?project_id=2", headers=self.origin,
            json={"status": "archived"}).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/settings/sources/{source_id}?project_id=1", headers=self.origin,
            json={"status": "archived"}).status_code, 200)
        added = self.client.post("/api/settings/reasons?project_id=1", headers=self.origin,
            json={"label": "Нет сроков"})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(self.client.patch(f"/api/settings/reasons/{added.json()['id']}?project_id=1",
            headers=self.origin, json={"status": "archived"}).status_code, 200)
        self.assertNotIn("Нет сроков", [r["label"] for r in self.client.get("/api/settings/reasons?project_id=1").json()])
        rule = self.client.patch("/api/settings/notifications/new_lead?project_id=1", headers=self.origin,
            json={"enabled": False, "in_app": True})
        self.assertEqual(rule.status_code, 200, rule.text)
        self.assertFalse(rule.json()["delivery_active"])
        self.assertEqual(self.client.patch("/api/settings/notifications/cpl_growth?project_id=1", headers=self.origin,
            json={"enabled": True, "in_app": True}).status_code, 422)
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin,
            json={"project_id": 1, "full_name": "Проверка причин", "phone": "+79001234567", "source": "Вручную"})
        self.assertEqual(lead.status_code, 201, lead.text)
        lost = self.client.post(f"/api/portal/crm/leads/{lead.json()['id']}/lost", headers=self.origin,
            json={"reason": "Не подходит"})
        self.assertEqual(lost.status_code, 200, lost.text)
        archived_reason = next(r for r in initial.json()["reasons"] if r["label"] == "Не подходит")
        self.assertEqual(self.client.patch(f"/api/settings/reasons/{archived_reason['id']}?project_id=1",
            headers=self.origin, json={"status": "archived"}).status_code, 200)
        today = datetime.now(timezone.utc).date().isoformat()
        analytic = self.client.get(f"/api/analytics?project_id=1&start={today}&end={today}")
        self.assertEqual(analytic.status_code, 200, analytic.text)
        self.assertIn({"label": "Не подходит", "count": 1}, analytic.json()["analysis"]["lost_reasons"])

    def test_client_cookie_takes_priority_over_admin_cookie_on_client_reports(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "owner-with-admin-cookie", "display_name": "Owner", "role": "client_owner"}).json()
        login = self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "owner-with-admin-cookie", "password": owner["temporary_password"]})
        self.assertEqual(login.status_code, 200, login.text)
        # The admin cookie from setUp remains present alongside the client cookie.
        self.assertIn("stl_session", self.client.cookies)
        self.assertIn("stl_portal_session", self.client.cookies)
        projects = self.client.get("/api/result/projects")
        self.assertEqual(projects.status_code, 200, projects.text)
        self.assertEqual([row["id"] for row in projects.json()], [1])
        self.assertEqual(self.client.get("/api/result?project_id=2").status_code, 404)
        self.assertEqual(self.client.get("/api/ads?project_id=2").status_code, 404)
        async def seed_accounts():
            async with self.sessions() as db:
                db.add_all([
                    AdConnection(workspace_id=1, project_id=1, platform="yandex", name="Own account",
                                 external_account_id="own", access_token_encrypted="test"),
                    AdConnection(workspace_id=2, project_id=2, platform="yandex", name="Foreign account",
                                 external_account_id="foreign", access_token_encrypted="test"),
                ])
                await db.commit()
        asyncio.run(seed_accounts())
        ads = self.client.get("/api/ads?project_id=1")
        self.assertEqual(ads.status_code, 200, ads.text)
        self.assertEqual([account["name"] for platform in ads.json()["platforms"]
                          for account in platform["accounts"]], ["Own account"])
        self.assertEqual(self.client.get("/api/settings?project_id=1").status_code, 200)
        self.assertEqual(self.client.get("/api/settings?project_id=2").status_code, 404)
        self.assertEqual(self.client.get("/api/result?project_id=1").json()["viewer"]["role"], "client_owner")

    def test_project_economics_is_saved_once_and_scoped_to_owner(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "economics-owner", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin, json={
            "username": "economics-owner", "password": owner["temporary_password"]})
        endpoint = "/api/result/projects/1/economics/model"
        payload = {"average_order_value": 50000, "purchases_per_customer": 2, "gross_margin": 40,
                   "fixed_costs": 100000, "current_customers": 10, "target_customers": 20,
                   "growth_budget": 50000, "allowable_cac": 12000}
        self.assertEqual(self.client.get("/api/result/projects/2/economics/model").status_code, 404)
        self.assertEqual(self.client.put("/api/result/projects/2/economics/model", headers=self.origin,
                                         json=payload).status_code, 404)
        self.assertEqual(self.client.put(endpoint, headers=self.origin,
                                         json={"average_order_value": 1}).status_code, 422)
        saved = self.client.put(endpoint, headers=self.origin, json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get(endpoint).json()["inputs"]["gross_margin"], 40)
        self.assertEqual(self.client.get("/api/result?project_id=1").json()["economics"]["allowable_cac"], 12000)
        payload["gross_margin"] = 50
        self.assertEqual(self.client.put(endpoint, headers=self.origin, json=payload).status_code, 200)
        self.assertEqual(self.client.get(endpoint).json()["inputs"]["gross_margin"], 50)
        async def count_rows():
            async with self.sessions() as db:
                from sqlalchemy import func, select
                return await db.scalar(select(func.count()).select_from(ProjectEconomics).where(
                    ProjectEconomics.project_id == 1))
        self.assertEqual(asyncio.run(count_rows()), 1)

    def test_team_is_organization_scoped_and_permissions_are_enforced(self):
        first = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "owner-one@test.example", "display_name": "Owner One",
            "role": "client_owner"}).json()
        second = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 2, "username": "owner-two@test.example", "display_name": "Owner Two",
            "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/portal/auth/login", headers=self.origin,
            json={"username": "owner-one@test.example", "password": first["temporary_password"]}).status_code, 200)
        team = self.client.get("/api/team")
        self.assertEqual(team.status_code, 200, team.text)
        self.assertEqual([member["id"] for member in team.json()["members"]], [first["id"]])
        self.assertEqual(self.client.get(f"/api/team/{second['id']}").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/team/{second['id']}", headers=self.origin,
            json={"display_name": "Wrong"}).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/team/{first['id']}", headers=self.origin,
            json={"status": "blocked"}).status_code, 409)
        invite = self.client.post("/api/team", headers=self.origin, json={
            "display_name": "Read Only", "email": "viewer@test.example", "role": "viewer"})
        self.assertEqual(invite.status_code, 201, invite.text)
        self.assertEqual(invite.json()["status"], "invited")
        self.assertEqual(invite.json()["delivery"], "manual")
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/portal/auth/login", headers=self.origin,
            json={"username": "viewer@test.example", "password": invite.json()["temporary_password"]}).status_code, 200)
        self.assertEqual(self.client.get("/api/team").status_code, 403)
        self.assertEqual(self.client.get("/api/ads?project_id=1").status_code, 200)
        self.assertEqual(self.client.post("/api/ads/connections", headers=self.origin, json={
            "project_id": 1, "platform": "yandex", "name": "Test", "external_account_id": "test",
            "access_token": "abcdefghij"}).status_code, 403)
        self.assertEqual(self.client.get("/api/result?project_id=2").status_code, 404)
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "owner-one@test.example", "password": first["temporary_password"]})
        updated = self.client.patch(f"/api/team/{invite.json()['id']}", headers=self.origin,
                                    json={"permissions": ["view_ads"]})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "viewer@test.example", "password": invite.json()["temporary_password"]})
        self.assertEqual(self.client.get("/api/result?project_id=1").status_code, 403)
        self.assertEqual(self.client.get("/api/portal/crm/leads").status_code, 403)
        self.assertEqual(self.client.get("/api/ads?project_id=1").status_code, 200)

    def test_empty_project_has_null_metrics_and_owner_cannot_read_other_organization(self):
        result = self.client.get("/api/result?project_id=1&start=2026-09-01&end=2026-09-30")
        self.assertEqual(result.status_code, 200, result.text)
        totals = result.json()["current"]["totals"]
        self.assertIsNone(totals["spend"])
        self.assertIsNone(totals["leads"])
        self.assertIsNone(totals["romi"])
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "owner-one", "display_name": "Owner One", "role": "client_owner"})
        self.assertEqual(owner.status_code, 201, owner.text)
        self.client.cookies.clear()
        login = self.client.post("/api/portal/auth/login", headers=self.origin,
                                 json={"username": "owner-one", "password": owner.json()["temporary_password"]})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(self.client.get("/api/result?project_id=2").status_code, 404)
        self.assertEqual(self.client.get("/api/analytics?project_id=2").status_code, 404)
        self.assertEqual([row["id"] for row in self.client.get("/api/result/projects").json()], [1])

    def test_spend_lead_qualification_and_multiple_sales_use_shared_records(self):
        async def seed_ad():
            async with self.sessions() as db:
                db.add(AdConnection(id=1, workspace_id=1, project_id=1, platform="yandex", name="Яндекс",
                                    external_account_id="123", access_token_encrypted="test", status="connected"))
                db.add(AdMetricDaily(connection_id=1, date=datetime.now(timezone.utc).date(), spend=1000,
                                     impressions=100, clicks=10, leads=999))
                db.add(AdCampaignMetricDaily(connection_id=1, date=datetime.now(timezone.utc).date(),
                                             external_campaign_id="campaign-1", campaign_name="Real Campaign",
                                             spend=1000, impressions=100, clicks=10))
                db.add(GrowthCalculation(id=1, name="Economics", inputs={"average_order_value": 10000, "gross_margin": 40},
                                         results={"metrics": {"breakEvenCustomersRounded": 4}}, status="TEST_READY"))
                await db.commit()
        asyncio.run(seed_ad())
        self.assertEqual(self.client.put("/api/result/projects/1/economics", headers=self.origin,
                          json={"growth_calculation_id": 1, "allowable_cac": 700}).status_code, 200)
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "head-one", "display_name": "Sales Head", "role": "sales_head"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "head-one", "password": owner["temporary_password"]})
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin,
                                json={"full_name": "Real Person", "phone": "+79000000000", "source": "Вручную"})
        self.assertEqual(lead.status_code, 201, lead.text)
        lead_id = lead.json()["id"]
        self.assertEqual(self.client.patch(f"/api/portal/crm/leads/{lead_id}", headers=self.origin,
                                           json={"status": "qualified"}).status_code, 200)
        for amount in (1200, 1800):
            self.assertEqual(self.client.post(f"/api/portal/crm/leads/{lead_id}/sales", headers=self.origin,
                                              json={"amount": amount}).status_code, 201)
        today = datetime.now(timezone.utc).date().isoformat()
        result = self.client.get(f"/api/result?project_id=1&start={today}&end={today}")
        self.assertEqual(result.status_code, 200, result.text)
        totals = result.json()["current"]["totals"]
        self.assertEqual(totals["spend"], 1000)
        self.assertEqual(totals["leads"], 1)  # AdMetricDaily.leads=999 is not a real lead.
        self.assertEqual(totals["qualified"], 1)
        self.assertEqual(totals["sales"], 2)
        self.assertEqual(totals["revenue"], 3000)
        self.assertEqual(totals["cpl"], 1000)
        self.assertEqual(totals["cac"], 500)
        self.assertAlmostEqual(totals["romi"], 20)
        self.assertEqual(self.client.get(f"/api/result/leads?project_id=1").json()["rows"][0]["sales_count"], 2)
        analytics = self.client.get(f"/api/analytics?project_id=1&start={today}&end={today}")
        self.assertEqual(analytics.status_code, 200, analytics.text)
        self.assertEqual(analytics.json()["current"]["totals"], totals)
        self.assertEqual(analytics.json()["analysis"]["current"]["clicks"], 10)
        self.assertIsNone(analytics.json()["analysis"]["campaigns"][0]["leads"])

        async def attribute_lead():
            async with self.sessions() as db:
                db.add(ClientLeadAttribution(lead_id=lead_id, connection_id=1,
                                             external_campaign_id="campaign-1"))
                await db.commit()
        asyncio.run(attribute_lead())
        attributed = self.client.get(f"/api/analytics?project_id=1&start={today}&end={today}")
        self.assertEqual(attributed.status_code, 200, attributed.text)
        campaign = attributed.json()["analysis"]["campaigns"][0]
        self.assertEqual((campaign["leads"], campaign["qualified"], campaign["sales"], campaign["revenue"]),
                         (1, 1, 2, 3000))

    def test_project_access_is_enforced_in_result_and_existing_crm(self):
        created = self.client.post("/api/result/projects", headers=self.origin,
                                   json={"organization_id": 1, "name": "Restricted"})
        self.assertEqual(created.status_code, 201, created.text)
        restricted_id = created.json()["id"]

        async def seed_lead():
            async with self.sessions() as db:
                db.add(ClientLead(workspace_id=1, project_id=restricted_id, full_name="Hidden",
                                  phone="+79001111111", source="manual"))
                await db.commit()
        asyncio.run(seed_lead())
        created_user = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "restricted-head", "display_name": "Head", "role": "sales_head"})
        self.assertEqual(created_user.status_code, 201, created_user.text)
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "restricted-head", "password": created_user.json()["temporary_password"]})
        self.assertEqual(self.client.get(f"/api/result?project_id={restricted_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/portal/crm/leads").json(), [])

    def test_sale_without_amount_does_not_invent_revenue(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "owner-unknown", "display_name": "Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        login = self.client.post("/api/portal/auth/login", headers=self.origin,
                                 json={"username": "owner-unknown", "password": owner["temporary_password"]})
        self.assertEqual(login.status_code, 200, login.text)
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin, json={
            "full_name": "Person", "phone": "+79002222222", "source": "manual"}).json()
        updated = self.client.patch(f"/api/portal/crm/leads/{lead['id']}", headers=self.origin,
                                    json={"status": "won"})
        self.assertEqual(updated.status_code, 422, updated.text)
        async def seed_legacy_sale():
            async with self.sessions() as db:
                row = await db.get(ClientLead, lead["id"])
                row.status = "won"
                row.qualified_at = datetime.now(timezone.utc)
                db.add(ClientSale(lead_id=row.id, project_id=row.project_id, amount=None,
                                  occurred_at=datetime.now(timezone.utc)))
                await db.commit()
        asyncio.run(seed_legacy_sale())
        today = datetime.now(timezone.utc).date().isoformat()
        totals = self.client.get(f"/api/result?project_id=1&start={today}&end={today}").json()["current"]["totals"]
        self.assertEqual(totals["sales"], 1)
        self.assertIsNone(totals["revenue"])

    def test_lead_register_actions_share_result_facts_and_events(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "lead-owner", "display_name": "Lead Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "lead-owner", "password": owner["temporary_password"]})
        created = self.client.post("/api/portal/crm/leads", headers=self.origin, json={
            "project_id": 1, "full_name": "Telegram Contact", "telegram": "@realcontact", "source": "Рекомендация"})
        self.assertEqual(created.status_code, 201, created.text)
        lead_id = created.json()["id"]
        today = datetime.now(timezone.utc).date().isoformat()
        register = self.client.get(f"/api/result/leads?project_id=1&start={today}&end={today}&search=realcontact&page_size=1")
        self.assertEqual(register.status_code, 200, register.text)
        self.assertEqual(register.json()["total"], 1)
        self.assertEqual(register.json()["rows"][0]["status"], "lead")
        self.assertEqual(self.client.patch(f"/api/portal/crm/leads/{lead_id}", headers=self.origin,
                                           json={"status": "qualified"}).status_code, 200)
        bad_sale = self.client.post(f"/api/portal/crm/leads/{lead_id}/sales", headers=self.origin,
                                    json={"amount": 0})
        self.assertEqual(bad_sale.status_code, 422)
        sold = self.client.post(f"/api/portal/crm/leads/{lead_id}/sales", headers=self.origin,
                                json={"amount": 12000, "comment": "Подтверждено ОП"})
        self.assertEqual(sold.status_code, 201, sold.text)
        self.assertEqual(self.client.post(f"/api/portal/crm/leads/{lead_id}/comments", headers=self.origin,
                                          json={"text": "Повторная покупка возможна"}).status_code, 201)
        totals = self.client.get(f"/api/result?project_id=1&start={today}&end={today}").json()["current"]["totals"]
        self.assertEqual((totals["leads"], totals["qualified"], totals["sales"], totals["revenue"]), (1, 1, 1, 12000))
        detail = self.client.get(f"/api/result/leads/{lead_id}?project_id=1").json()
        self.assertEqual(len(detail["sales"]), 1)
        self.assertTrue({"LEAD_CREATED", "LEAD_QUALIFIED", "SALE_CREATED", "COMMENT_ADDED"}.issubset(
            {event["event_type"] for event in detail["events"]}))
        self.assertEqual(self.client.post(f"/api/portal/crm/leads/{lead_id}/lost", headers=self.origin,
                                          json={"reason": "Не подходит"}).status_code, 422)

    def test_lost_requires_reason_and_preserves_lead(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "lost-owner", "display_name": "Lost Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "lost-owner", "password": owner["temporary_password"]})
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin, json={
            "project_id": 1, "full_name": "Lost Person", "email": "lost@example.com"}).json()
        invalid = self.client.post(f"/api/portal/crm/leads/{lead['id']}/lost", headers=self.origin,
                                   json={"reason": "Другое"})
        self.assertEqual(invalid.status_code, 422)
        lost = self.client.post(f"/api/portal/crm/leads/{lead['id']}/lost", headers=self.origin,
                                json={"reason": "Другое", "detail": "Не готов к покупке"})
        self.assertEqual(lost.status_code, 200, lost.text)
        register = self.client.get("/api/result/leads?project_id=1&status=lost").json()
        self.assertEqual(register["total"], 1)
        self.assertEqual(register["rows"][0]["lost_reason"], "Другое: Не готов к покупке")

    def test_sales_register_uses_confirmed_sales_and_same_result_totals(self):
        owner = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "sales-owner", "display_name": "Sales Owner", "role": "client_owner"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "sales-owner", "password": owner["temporary_password"]})
        lead = self.client.post("/api/portal/crm/leads", headers=self.origin, json={
            "project_id": 1, "full_name": "Buyer", "email": "buyer@example.com", "source": "Рекомендация"}).json()
        for amount, comment in ((10000, "Первая покупка"), (2500, "Повторная покупка")):
            saved = self.client.post(f"/api/portal/crm/leads/{lead['id']}/sales", headers=self.origin,
                                     json={"amount": amount, "comment": comment})
            self.assertEqual(saved.status_code, 201, saved.text)
        today = datetime.now(timezone.utc).date().isoformat()
        register = self.client.get(f"/api/result/sales?project_id=1&start={today}&end={today}&page_size=1")
        self.assertEqual(register.status_code, 200, register.text)
        self.assertEqual(register.json()["total"], 2)
        self.assertEqual(register.json()["cycle_days"], 0)
        self.assertEqual(register.json()["rows"][0]["source"], "Рекомендация")
        self.assertEqual(register.json()["rows"][0]["comment"], "Повторная покупка")
        self.assertEqual(register.json()["rows"][0]["lead_id"], lead["id"])
        filtered = self.client.get(f"/api/result/sales?project_id=1&start={today}&end={today}&min_amount=9000")
        self.assertEqual(filtered.json()["total"], 1)
        totals = self.client.get(f"/api/result?project_id=1&start={today}&end={today}").json()["current"]["totals"]
        self.assertEqual((totals["sales"], totals["revenue"], totals["average_check"]), (2, 12500, 6250))
        detail = self.client.get(f"/api/result/leads/{lead['id']}?project_id=1").json()
        self.assertEqual(len(detail["sales"]), 2)
        self.assertEqual(detail["lead"]["sales_count"], 2)
        self.assertEqual(self.client.get(f"/api/result/sales?project_id=2").status_code, 404)

    def test_ads_only_real_ad_connections_and_attributed_leads(self):
        today = datetime.now(timezone.utc).date()
        async def seed():
            async with self.sessions() as db:
                db.add(AdConnection(id=11, workspace_id=1, project_id=1, platform="yandex", name="Кабинет А",
                                    external_account_id="client-a", access_token_encrypted="secret", status="connected"))
                db.add(AdMetricDaily(connection_id=11, date=today, spend=1000, impressions=100,
                                     clicks=10, leads=999))
                db.add(ProjectSource(id=11, project_id=1, name="Telegram Outreach", kind="INTERNAL", method="INTERNAL"))
                db.add(SourceMetricDaily(source_id=11, date=today, spend=500, aggregated_leads=5))
                db.add(GrowthCalculation(id=11, name="Model", inputs={"gross_margin": 40},
                                         results={"metrics": {}}, status="TEST_READY"))
                db.add(ProjectEconomics(project_id=1, growth_calculation_id=11))
                await db.commit()
                lead = ClientLead(workspace_id=1, project_id=1, full_name="Ad Buyer", phone="+79000000001",
                                  source="Яндекс")
                db.add(lead); await db.flush()
                db.add(ClientLeadAttribution(lead_id=lead.id, connection_id=11, external_campaign_id="campaign-a"))
                db.add(ClientSale(lead_id=lead.id, project_id=1, amount=3000, occurred_at=datetime.now(timezone.utc)))
                await db.commit()
        asyncio.run(seed())
        day = today.isoformat()
        response = self.client.get(f"/api/ads?project_id=1&start={day}&end={day}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual([platform["name"] for platform in data["platforms"]], ["Яндекс Директ"])
        self.assertEqual(data["current"]["spend"], 1000)
        self.assertEqual(data["current"]["clicks"], 10)
        self.assertEqual(data["current"]["leads"], 1)
        self.assertEqual(data["current"]["cpc"], 100)
        self.assertEqual(data["current"]["cpl"], 1000)
        self.assertEqual(data["current"]["romi"], 20)
        self.assertEqual(self.client.get(f"/api/ads?project_id=2&start={day}&end={day}").json()["platforms"], [])
        disconnected = self.client.delete("/api/marketing/connections/11", headers=self.origin)
        self.assertEqual(disconnected.status_code, 204, disconnected.text)
        after = self.client.get(f"/api/ads?project_id=1&start={day}&end={day}").json()
        self.assertEqual(after["platforms"][0]["status"], "disconnected")
        self.assertEqual(after["current"]["spend"], 1000)

    def test_ads_manage_integrations_is_project_permission(self):
        marketer = self.client.post("/api/portal/admin/users", headers=self.origin, json={
            "workspace_id": 1, "username": "ads-marketer", "display_name": "Ads Marketer",
            "role": "client_marketer"}).json()
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "ads-marketer", "password": marketer["temporary_password"]})
        payload = {"project_id": 1, "platform": "yandex", "name": "Client account",
                   "external_account_id": "client", "access_token": "valid-looking-token"}
        self.assertEqual(self.client.post("/api/ads/connections", headers=self.origin, json=payload).status_code, 403)
        self.client.cookies.clear(); self.client.cookies.set("stl_session", "a" * 43)
        granted = self.client.put("/api/result/projects/1/access", headers=self.origin,
                                  json={"user_id": marketer["id"], "manage_integrations": True})
        self.assertEqual(granted.status_code, 200, granted.text)
        self.client.cookies.clear()
        self.client.post("/api/portal/auth/login", headers=self.origin,
                         json={"username": "ads-marketer", "password": marketer["temporary_password"]})
        created = self.client.post("/api/ads/connections", headers=self.origin, json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(self.client.get("/api/ads?project_id=2").status_code, 404)

    def test_manual_source_without_clicks_has_no_click_conversion(self):
        created = self.client.post("/api/result/sources", headers=self.origin, json={
            "project_id": 1, "name": "Offline", "kind": "MANUAL", "method": "MANUAL"})
        self.assertEqual(created.status_code, 201, created.text)
        source_id = int(created.json()["id"].split(":")[1])
        today = datetime.now(timezone.utc).date().isoformat()
        saved = self.client.put(f"/api/result/sources/{source_id}/daily", headers=self.origin, json={
            "date": today, "spend": 300, "aggregated_leads": 2})
        self.assertEqual(saved.status_code, 200, saved.text)
        result = self.client.get(f"/api/result?project_id=1&start={today}&end={today}").json()
        analytics = self.client.get(f"/api/analytics?project_id=1&start={today}&end={today}").json()
        self.assertEqual(analytics["current"]["totals"], result["current"]["totals"])
        self.assertIsNone(analytics["analysis"]["current"]["clicks"])
        self.assertIsNone(analytics["analysis"]["current"]["sources"][0]["click_to_lead"])


if __name__ == "__main__":
    unittest.main()
