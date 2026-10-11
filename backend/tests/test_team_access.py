"""Rights: only the owner changes employees; a sales manager works only with own deals and clients."""
import unittest

from sqlalchemy import update

import test_stage2
from app.core.access import hash_password, token_digest
from app.models.crm import CrmDeal
from app.core.permissions import ROLE_DEFAULTS
from app.models.marketing import PortalProjectAccess, PortalSession, PortalUser
from datetime import datetime, timezone

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class TeamAccessTests(unittest.TestCase):
    setUp_base, tearDown, as_user, run_db, lead = Base.setUp, Base.tearDown, Base.as_user, Base.run_db, Base.lead

    def setUp(self):
        self.setUp_base()

        async def more_users(db):
            for uid, role in ((3, "sales_head"), (4, "sales_manager")):
                db.add(PortalUser(id=uid, workspace_id=1, username=f"u{uid}", display_name=f"User {uid}",
                                  password_hash=hash_password("test-password-123"), role=role, must_change_password=False,
                                  # The head is also given access to the team section: still read-only there.
                                  permissions=sorted({*ROLE_DEFAULTS[role], "manage_team"}) if role == "sales_head" else None))
                db.add(PortalSession(token_hash=token_digest(str(uid) * 43), user_id=uid,
                                     expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
            await db.flush()
            db.add_all([PortalProjectAccess(user_id=3, project_id=1), PortalProjectAccess(user_id=4, project_id=1)])
            await db.commit()
        self.run_db(more_users)

    def assign(self, deal_id, uid):
        async def go(db):
            await db.execute(update(CrmDeal).where(CrmDeal.id == deal_id).values(responsible_user_id=uid)); await db.commit()
        self.run_db(go)

    def test_only_owner_changes_team(self):
        self.as_user(3)
        self.assertEqual(self.client.get("/api/team").status_code, 200)
        invite = self.client.post("/api/team", headers=ORIGIN, json={"display_name": "Новый", "email": "n@x.ru", "role": "sales_manager"})
        self.assertEqual(invite.status_code, 403, invite.text)
        self.assertIn("только владелец", invite.json()["detail"])
        self.assertEqual(self.client.patch("/api/team/4", headers=ORIGIN, json={"display_name": "Другое имя"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/team/4", headers=ORIGIN).status_code, 403)
        self.as_user(1)
        self.assertEqual(self.client.patch("/api/team/4", headers=ORIGIN, json={"display_name": "Менеджер Четыре"}).status_code, 200)

    def test_manager_edits_only_own_deals_head_edits_all(self):
        mine, theirs = self.lead("Анна", "89001110001", "t1"), self.lead("Борис", "89001110002", "t2")
        self.assign(mine, 2); self.assign(theirs, 4)
        self.as_user(2)
        self.assertEqual(self.client.patch(f"/api/crm/deals/{mine}", headers=ORIGIN, json={"name": "Моя сделка"}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/crm/deals/{theirs}", headers=ORIGIN, json={"name": "Чужая"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/crm/deals/{theirs}").status_code, 403)
        # Contacts of a colleague's deal are neither listed nor editable.
        names = [c["name"] for c in self.client.get("/api/crm/projects/1/contacts").json()["items"]]
        self.assertIn("Анна", names); self.assertNotIn("Борис", names)
        their_contact = self.run_db(lambda db: db.get(CrmDeal, theirs)).contact_id
        self.assertEqual(self.client.get(f"/api/crm/contacts/{their_contact}").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/crm/contacts/{their_contact}", headers=ORIGIN, json={"name": "Взлом"}).status_code, 403)
        # The sales head edits any deal without asking the manager.
        self.as_user(3)
        self.assertEqual(self.client.patch(f"/api/crm/deals/{theirs}", headers=ORIGIN, json={"name": "Правка РОПа"}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/crm/contacts/{their_contact}", headers=ORIGIN, json={"name": "Борис Б."}).status_code, 200)

