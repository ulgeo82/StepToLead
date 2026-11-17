"""Этап D: клиентский аутрич только для чтения."""
import unittest
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from unittest.mock import patch

import test_stage2

from app.core.access import COOKIE, PORTAL_COOKIE, token_digest
from app.core.config import settings
from app.domains.leadgen.models import LgCampaign, LgCompany, LgMailbox, LgSequence, LgTouch
from app.models import Campaign, Lead, TelegramAccount
from app.models.access import AdminSession
from app.models.automation import CampaignDialog, CampaignMessage
from app.models.marketing import ClientWorkspace, PortalSession, PortalUser

BASE = "/api/portal/outreach"
ME = "/api/portal/auth/me"
ORIGIN = test_stage2.ORIGIN
START = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)


class PortalOutreachTests(unittest.TestCase):
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        test_stage2.Stage2Tests.setUp(self)
        self.addCleanup(test_stage2.Stage2Tests.tearDown, self)
        _p = patch.object(settings, "agency_workspace_id", None)
        _p.start()
        self.addCleanup(_p.stop)
        self.other_client_session = token_urlsafe(32)
        self.admin_session = token_urlsafe(32)

        async def seed(db):
            db.add(ClientWorkspace(id=2, name="Второй клиент", show_outreach=True))
            db.add(PortalUser(id=3, workspace_id=2, username="other-owner", display_name="Сотрудник", password_hash="",
                              role="client_owner", must_change_password=False))
            db.add(PortalSession(user_id=3, token_hash=token_digest(self.other_client_session),
                                 expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
            db.add(AdminSession(user_id=1, token_hash=token_digest(self.admin_session),
                                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
            for ws, company in ((1, "Кухни"), (2, "Другой клиент")):
                db.add(LgCompany(id=ws, workspace_id=ws, display_name=company))
                db.add(LgMailbox(id=ws, workspace_id=ws, email=f"sales{ws}@example.org", sender_name=company,
                                 smtp_host="mail.example.org", imap_host="mail.example.org",
                                 login=f"sales{ws}", password_encrypted=""))
                db.add(LgSequence(id=ws, workspace_id=ws, name=company))
                db.add(LgCampaign(workspace_id=ws, sequence_id=ws, name=f"Email: {company}"))
                db.add(Campaign(id=ws, workspace_id=ws, name=f"Telegram: {company}"))
                db.add(TelegramAccount(id=ws, workspace_id=ws, phone=f"+7900000000{ws}", session_encrypted="",
                                       username=None if ws == 1 else "other_sender"))
            await db.flush()
            for ws in (1, 2):
                db.add(Lead(id=ws, campaign_id=ws, username=f"customer_{ws}", first_message="Здравствуйте"))
                await db.flush()
                db.add(CampaignDialog(id=ws, campaign_id=ws, lead_id=ws, account_id=ws, peer_id=500 + ws, access_hash=1,
                                      username=f"customer_{ws}", state="active"))
                await db.flush()
                for i, (kind, status, body) in enumerate((
                    ("first", "sent", "Здравствуйте"), ("incoming", "received", "Пришлите условия"),
                    ("handoff", "sent", "Служебное событие"), ("system", "received", "Служебное сообщение"),
                )):
                    db.add(CampaignMessage(campaign_id=ws, dialog_id=ws, account_id=ws, kind=kind,
                                           body=body, status=status, dedupe_key=f"test:{i}",
                                           created_at=START + timedelta(hours=i)))
                for i, (direction, status, label, body) in enumerate((
                    ("out", "sent", None, "Здравствуйте"), ("in", "received", "interested", "Какова цена?"),
                    ("in", "received", "auto", "Автоответ"), ("out", "planned", None, "Черновик"),
                )):
                    db.add(LgTouch(id=ws * 10 + i, company_id=ws, mailbox_id=ws, channel="email",
                                   direction=direction, status=status, label=label, body=body,
                                   summary="Внутренняя сводка: provider_model", address=f"info{ws}@example.net",
                                   hot_at=START if label == "interested" else None, campaign_kind="sequence",
                                   campaign_id=ws, sequence_id=str(ws), happened_at=START + timedelta(minutes=i)))
            await db.commit()

        self.run_db(seed)

    def get(self, path, **kwargs):
        res = self.client.get(path, **kwargs)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def enable(self):
        async def set_flag(db):
            (await db.get(ClientWorkspace, 1)).show_outreach = True
            await db.commit()
        self.run_db(set_flag)

    def test_switch_is_for_agency_and_defaults_off(self):
        self.assertFalse(self.get(ME)["show_outreach"])
        for path in ("/summary", "/contacted", "/inbox", "/inbox/email:11"):
            self.assertEqual(self.client.get(BASE + path).status_code, 403)
        url = "/api/marketing/workspaces/1/outreach"
        payload = {"show_outreach": True}
        self.assertIn(self.client.patch(url, headers=ORIGIN, json=payload).status_code, (401, 403))
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, self.admin_session)
        self.assertEqual(self.client.patch(url, headers={"Origin": "https://example.org"}, json=payload).status_code, 403)
        self.assertEqual(self.client.patch(url, headers=ORIGIN, json=payload).json(), payload)
        self.as_user(2)
        self.assertTrue(self.get(ME)["show_outreach"])
        self.get(f"{BASE}/summary")

    def test_scope_summary_and_contacted(self):
        self.enable()
        self.as_user(2)
        forged = {"X-Workspace-Id": "2"}
        expected = {"companies": 1, "people": 2, "sent": 2, "replied": 2, "hot": 1}
        self.assertEqual(self.get(f"{BASE}/summary?ws=2", headers=forged), expected)
        rows = self.get(f"{BASE}/contacted?ws=2", headers=forged)
        self.assertEqual(rows["total"], 2)
        self.assertEqual({r["contact"] for r in rows["items"]}, {"info1@example.net", "@customer_1"})
        email = self.get(f"{BASE}/contacted", params={"channel": "email", "q": "кухни"})["items"][0]
        self.assertEqual((email["campaign"], email["status"]), ("Email: Кухни", "interested"))
        self.assertEqual(datetime.fromisoformat(email["first_contact_at"]), START)
        self.assertEqual(self.get(f"{BASE}/contacted?page=3&per_page=1")["items"], [])
        result = self.get(f"{BASE}/inbox?ws=2", headers=forged)
        self.assertEqual({r["key"] for r in result["items"]}, {"email:11", "tg:1"})
        for key in ("email:21", "tg:2"):
            self.assertEqual(self.client.get(f"{BASE}/inbox/{key}?ws=2", headers=forged).status_code, 404)
        self.client.cookies.clear()
        self.client.cookies.set(PORTAL_COOKIE, self.other_client_session)
        self.assertEqual({r["key"] for r in self.get(f"{BASE}/inbox")["items"]}, {"email:21", "tg:2"})
        self.assertEqual(self.get(f"{BASE}/inbox/tg:2")["sender"], "@other_sender")
        self.assertEqual(self.client.get(f"{BASE}/inbox/tg:1").status_code, 404)

    def test_public_messages_and_reserved_permission(self):
        self.enable()

        async def reserve(db):
            user = await db.get(PortalUser, 1)
            self.assertFalse(user.can_reply_outreach)
            user.can_reply_outreach = True
            await db.commit()
        self.run_db(reserve)
        for key, sender in (("email:11", "sales1@example.org"), ("tg:1", None)):
            detail = self.get(f"{BASE}/inbox/{key}")
            self.assertEqual(set(detail), {"key", "channel", "campaign", "contact", "sender",
                                           "last", "label", "hot", "messages"})
            self.assertEqual(detail["sender"], sender)
            self.assertEqual([msg["author"] for msg in detail["messages"]], ["Агентство", "Лид"])
            self.assertEqual(len(detail["messages"]), 2)
            for forbidden in ("proxy", "phone", "+79000000001", "password", "provider_model",
                              "sales2@example.org", "Служебное", "Автоответ", "Черновик"):
                self.assertNotIn(forbidden, str(detail))
            path = f"{BASE}/inbox/{key}"
            self.assertEqual(self.client.patch(path, headers=ORIGIN, json={"label": "interested"}).status_code, 405)
            for action in ("reply", "claim", "assign", "lock", "suggest", "to-crm"):
                self.assertIn(self.client.post(f"{path}/{action}", headers=ORIGIN, json={}).status_code, (404, 405))
        self.assertEqual(self.client.get(f"{BASE}/inbox/email:12").status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"{BASE}/summary").status_code, 401)

    def test_cross_workspace_sender_is_hidden(self):
        self.enable()

        async def link_bad_senders(db):
            (await db.get(LgTouch, 11)).mailbox_id = 2
            (await db.get(CampaignDialog, 1)).account_id = 2
            await db.commit()
        self.run_db(link_bad_senders)

        result = self.get(f"{BASE}/inbox")
        self.assertEqual([row["sender"] for row in result["items"]], [None, None])
        for key in ("email:11", "tg:1"):
            self.assertIsNone(self.get(f"{BASE}/inbox/{key}")["sender"])
