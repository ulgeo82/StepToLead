"""Что доступно клиенту, а что только StepToLead: роли собственник / РОП / МОП / наблюдатель и сотрудник агентства."""
import unittest
from types import SimpleNamespace

import test_stage2
from app.core.permissions import AGENCY_ONLY, CAPABILITIES, effective_permissions
from app.models.marketing import PortalUser

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


def person(role, **extra):
    return SimpleNamespace(role=role, permissions=extra.pop("permissions", None), is_agency=extra.pop("is_agency", False),
                           manage_sources=False, manage_integrations=False, **extra)


class PolicyTests(unittest.TestCase):
    def test_client_roles_never_get_agency_rights(self):
        for role in ("client_owner", "sales_head", "sales_manager", "viewer", "client_marketer"):
            self.assertFalse(effective_permissions(person(role)) & AGENCY_ONLY, role)
        # Даже выставленные вручную технические права вычищаются.
        custom = person("viewer", permissions=["view_result", "manage_integrations", "view_ads", "manage_reports"])
        self.assertEqual(effective_permissions(custom), {"view_result"})
        self.assertEqual(effective_permissions(person("client_owner", is_agency=True)), set(CAPABILITIES))

    def test_hierarchy(self):
        owner, head, manager, viewer = (effective_permissions(person(r)) for r in ("client_owner", "sales_head", "sales_manager", "viewer"))
        self.assertIn("manage_team", owner)
        self.assertIn("exclude_analytics", head)
        self.assertIn("manage_pipeline", head)
        self.assertNotIn("exclude_analytics", manager)
        self.assertIn("view_own_deals", manager)
        self.assertNotIn("view_all_deals", manager)
        self.assertFalse(viewer & {"edit_deal", "manage_leads", "create_sale"})
        self.assertLess(viewer, owner)

    def test_demo_keeps_previous_rights(self):
        demo_owner = effective_permissions(person("client_owner", demo_mode=True))
        self.assertTrue({"manage_integrations", "view_ads", "manage_sources"} <= demo_owner)
        self.assertNotIn("manage_reports", demo_owner)


class ApiTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db, make_agency = Base.setUp, Base.tearDown, Base.as_user, Base.run_db, Base.make_agency

    def test_owner_is_refused_technical_setup_and_agency_is_allowed(self):
        self.as_user(1)  # собственник клиента
        self.assertEqual(self.client.get("/api/ads?project_id=1").status_code, 403)
        self.assertEqual(self.client.post("/api/settings/sources?project_id=1", headers=ORIGIN,
                                          json={"name": "Партнёры", "kind": "MANUAL", "method": "MANUAL"}).status_code, 403)
        self.assertEqual(self.client.post("/api/reports/preview", headers=ORIGIN,
                                          json={"project_id": 1, "start": "2026-09-01", "end": "2026-09-30"}).status_code, 403)
        self.assertEqual(self.client.get("/api/reports?project_id=1").status_code, 200)
        self.assertEqual(self.client.get("/api/team").status_code, 200)
        self.assertEqual(self.client.post("/api/settings/reasons?project_id=1", headers=ORIGIN, json={"label": "Дорого"}).status_code, 201)
        self.make_agency(1)
        self.as_user(1)
        self.assertEqual(self.client.get("/api/ads?project_id=1").status_code, 200)
        self.assertEqual(self.client.post("/api/reports/preview", headers=ORIGIN,
                                          json={"project_id": 1, "start": "2026-09-01", "end": "2026-09-30"}).status_code, 200)

    def test_owner_cannot_touch_agency_member_or_grant_marketer(self):
        async def add(db):
            db.add(PortalUser(id=9, workspace_id=1, username="stl", display_name="StepToLead", role="client_owner",
                              is_agency=True, password_hash="x", must_change_password=False))
            await db.commit()
        self.run_db(add)
        self.as_user(1)
        team = self.client.get("/api/team").json()
        self.assertEqual({m["id"]: m["role_name"] for m in team["members"]}[9], "Сотрудник StepToLead")
        self.assertNotIn("client_marketer", team["roles"])
        self.assertEqual(self.client.patch("/api/team/9", headers=ORIGIN, json={"display_name": "Чужой"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/team/9", headers=ORIGIN).status_code, 403)
        self.assertEqual(self.client.post("/api/team", headers=ORIGIN, json={
            "display_name": "Маркетолог", "email": "m@test.example", "role": "client_marketer"}).status_code, 422)
        self.assertEqual(self.client.post("/api/team", headers=ORIGIN, json={
            "display_name": "МОП", "email": "mop@test.example", "role": "sales_manager"}).status_code, 201)


if __name__ == "__main__":
    unittest.main()
