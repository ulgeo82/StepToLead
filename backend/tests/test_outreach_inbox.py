"""Этап C (docs/OUTREACH_INBOX.md): единые входящие аутрича — email + Telegram, по клиентам; назначение,
«пишу ответ» (lock), ответ с того же ящика/аккаунта, метки, шаблоны."""
import unittest
from datetime import datetime, timedelta, timezone

import test_stage2
from sqlalchemy import select

from app.core.access import COOKIE, hash_password, token_digest
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.domains.leadgen import routes as leadgen_routes
from app.domains.leadgen.models import LgCompany, LgMailbox, LgTouch
from app.models import Campaign, Lead, Proxy, TelegramAccount
from app.models.access import AdminSession, AdminUser
from app.models.automation import CampaignDialog, CampaignMessage, GlobalBlock
from app.models.marketing import ClientWorkspace
from app.services import outreach_inbox

ORIGIN = test_stage2.ORIGIN
HOME, CLIENT = 1, 2
AS_CLIENT = {**ORIGIN, "X-Workspace-Id": str(CLIENT)}
BASE = "/api/admin/outreach"
T0 = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, mb, pw, mail):
        self.sent.append(mail)


class OutreachInboxTests(unittest.TestCase):
    setUp_base = test_stage2.Stage2Tests.setUp
    run_db = test_stage2.Stage2Tests.run_db
    as_user = test_stage2.Stage2Tests.as_user

    def setUp(self):
        self.setUp_base()
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, "a" * 43)
        self.transport = Transport()
        self.tg_sent = []

        async def tg_send(account, dialog, text):
            self.tg_sent.append((account.id, dialog.peer_id, text))
            return 555
        self._old = (settings.agency_workspace_id, settings.encryption_secret, leadgen_routes.mail_transport_factory,
                     outreach_inbox.telegram_send, outreach_inbox.complete)
        settings.agency_workspace_id = HOME
        settings.encryption_secret = "test-secret-for-sessions"
        leadgen_routes.mail_transport_factory = lambda: self.transport
        outreach_inbox.telegram_send = tg_send

        async def seed(db):
            db.add(ClientWorkspace(id=CLIENT, name="OneSpot"))
            db.add(AdminUser(id=2, username="anna", display_name="Анна", role="admin",
                             password_hash=hash_password("test-password-123")))
            db.add(AdminSession(token_hash=token_digest("b" * 43), user_id=2,
                                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
            # email: агентство и клиент
            for ws, cid, mid, name in ((HOME, 101, 201, "Кухни Плюс"), (CLIENT, 102, 202, "Офис Центр")):
                db.add(LgCompany(id=cid, workspace_id=ws, display_name=name, domain=f"c{cid}.ru"))
                db.add(LgMailbox(id=mid, workspace_id=ws, email=f"egor@m{mid}.ru", sender_name="Егор",
                                 smtp_host="s", imap_host="i", login="egor", password_encrypted=encrypt_secret("p"),
                                 daily_limit=30))
            await db.flush()
            db.add(LgTouch(id=301, company_id=101, channel="email", direction="in", status="received",
                           subject="Re: Привет", body="Интересно, пришлите цены", address="info@c101.ru",
                           mailbox_id=201, happened_at=T0))
            db.add(LgTouch(id=302, company_id=102, channel="email", direction="in", status="received",
                           subject="Re: Привет", body="Сколько стоит?", address="info@c102.ru",
                           mailbox_id=202, happened_at=T0 + timedelta(minutes=5)))
            # Telegram: аккаунт клиента с прокси и диалог с ответом
            db.add(Proxy(id=12, host="10.2.2.2", port=1080, pool="agency", status="working", max_accounts=1,
                         workspace_id=CLIENT))
            db.add(TelegramAccount(id=22, phone="+79000000022", session_encrypted="x", proxy_id=12, status="online",
                                   workspace_id=CLIENT, username="onespot_anna"))
            db.add(TelegramAccount(id=23, phone="+79000000023", session_encrypted="x", proxy_id=None,
                                   status="online", workspace_id=CLIENT))
            db.add(Campaign(id=32, name="OneSpot коворкинги", workspace_id=CLIENT))
            await db.flush()
            db.add(Lead(id=41, campaign_id=32, username="lead_one", first_message="Привет"))
            db.add(Lead(id=42, campaign_id=32, username="lead_two", first_message="Привет"))
            await db.flush()
            db.add(CampaignDialog(id=51, campaign_id=32, lead_id=41, account_id=22, peer_id=9001, access_hash=1,
                                  username="lead_one", state="active", last_incoming_id=7))
            db.add(CampaignDialog(id=52, campaign_id=32, lead_id=42, account_id=23, peer_id=9002, access_hash=1,
                                  username="lead_two", state="active"))
            await db.flush()
            db.add(CampaignMessage(campaign_id=32, dialog_id=51, account_id=22, kind="first", body="Привет!",
                                   dedupe_key="first", status="sent", telegram_message_id=5))
            db.add(CampaignMessage(campaign_id=32, dialog_id=51, account_id=22, kind="incoming",
                                   body="А что по цене?", dedupe_key="in:7", status="received", telegram_message_id=7))
            db.add(CampaignMessage(campaign_id=32, dialog_id=52, account_id=23, kind="incoming",
                                   body="Да, интересно", dedupe_key="in:3", status="received", telegram_message_id=3))
            await db.commit()
        self.run_db(seed)

    def tearDown(self):
        (settings.agency_workspace_id, settings.encryption_secret, leadgen_routes.mail_transport_factory,
         outreach_inbox.telegram_send, outreach_inbox.complete) = self._old
        test_stage2.Stage2Tests.tearDown(self)

    def as_anna(self):
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, "b" * 43)

    def keys(self, params=None, headers=None):
        r = self.client.get(f"{BASE}/inbox", params=params or {}, headers=headers or {})
        self.assertEqual(r.status_code, 200, r.text)
        return sorted(i["key"] for i in r.json()["items"])

    def test_listing_is_per_client_and_all_clients_merges(self):
        self.assertEqual(self.keys(), ["email:301"])
        self.assertEqual(self.keys(headers=AS_CLIENT), ["email:302", "tg:51", "tg:52"])
        self.assertEqual(self.keys({"all_clients": 1}), ["email:301", "email:302", "tg:51", "tg:52"])
        self.assertEqual(self.keys({"channel": "telegram"}, AS_CLIENT), ["tg:51", "tg:52"])
        self.assertEqual(self.keys({"q": "lead_two"}, AS_CLIENT), ["tg:52"])
        item = next(i for i in self.client.get(f"{BASE}/inbox", headers=AS_CLIENT).json()["items"] if i["key"] == "tg:51")
        self.assertEqual(item["client"], {"id": CLIENT, "name": "OneSpot"})
        self.assertEqual(item["sender"]["label"], "@onespot_anna")
        self.assertEqual((item["last"]["direction"], item["unread"], item["handled"]), ("in", True, False))
        # чужой клиент — 404, не раскрываем
        self.assertEqual(self.client.get(f"{BASE}/inbox/email:301", headers=AS_CLIENT).status_code, 404)
        self.assertEqual(self.client.get(f"{BASE}/inbox/tg:51").status_code, 200)   # без выбора — из «всех клиентов»
        self.assertEqual(self.client.get(f"{BASE}/inbox/bad:1").status_code, 404)
        counter = self.client.get(f"{BASE}/inbox-counter", headers=AS_CLIENT).json()
        self.assertEqual((counter["unhandled"], counter["mine"]), (3, 0))
        self.assertEqual(self.client.get(f"{BASE}/inbox-counter").json()["unhandled"], 4)   # бейдж — все клиенты

    def test_claim_assign_and_lock(self):
        r = self.client.post(f"{BASE}/inbox/tg:51/claim", headers=AS_CLIENT)
        self.assertEqual(r.json()["assignee"], {"id": 1, "name": "admin"})
        self.assertEqual(self.keys({"status": "mine"}, AS_CLIENT), ["tg:51"])
        self.assertEqual(self.client.post(f"{BASE}/inbox/tg:51/assign", headers=AS_CLIENT,
                                          json={"user_id": 999}).status_code, 422)
        self.client.post(f"{BASE}/inbox/tg:51/assign", headers=AS_CLIENT, json={"user_id": 2})
        self.assertEqual(self.keys({"status": "mine"}, AS_CLIENT), [])
        ops = self.client.get(f"{BASE}/operators").json()
        self.assertEqual([o["name"] for o in ops], ["admin", "Анна"])

        self.assertEqual(self.client.post(f"{BASE}/inbox/tg:51/lock", headers=AS_CLIENT).status_code, 200)
        self.as_anna()
        r = self.client.post(f"{BASE}/inbox/tg:51/lock", headers=AS_CLIENT)
        self.assertEqual(r.status_code, 409)
        self.assertIn("admin", r.json()["detail"])
        r = self.client.post(f"{BASE}/inbox/tg:51/reply", headers=AS_CLIENT, json={"text": "Здравствуйте"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.tg_sent, [])
        item = self.client.get(f"{BASE}/inbox/tg:51", headers=AS_CLIENT).json()
        self.assertEqual(item["locked_by"]["id"], 1)
        # снял lock — Анна может писать
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)
        self.assertEqual(self.client.delete(f"{BASE}/inbox/tg:51/lock", headers=AS_CLIENT).status_code, 204)
        self.as_anna()
        self.assertEqual(self.client.post(f"{BASE}/inbox/tg:51/lock", headers=AS_CLIENT).status_code, 200)

    def test_telegram_reply_goes_from_same_account_and_stops_ai(self):
        r = self.client.post(f"{BASE}/inbox/tg:51/reply", headers=AS_CLIENT, json={"text": "Цена от 9 900 ₽"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["author"], {"kind": "agency", "name": "admin"})
        self.assertEqual(self.tg_sent, [(22, 9001, "Цена от 9 900 ₽")])

        async def check(db):
            dialog = await db.get(CampaignDialog, 51)
            row = await db.scalar(select(CampaignMessage).where(CampaignMessage.kind == "manual"))
            return dialog.state, dialog.last_reply_id, row.status, row.author_id, row.telegram_message_id
        self.assertEqual(self.run_db(check), ("manual", 7, "sent", 1, 555))
        full = self.client.get(f"{BASE}/inbox/tg:51", headers=AS_CLIENT).json()
        self.assertTrue(full["handled"])
        self.assertEqual([m["author"]["kind"] for m in full["messages"]], ["auto", "lead", "agency"])
        self.assertEqual(full["assignee"]["id"], 1)
        # новое входящее — снова неразобрано
        async def incoming(db):
            db.add(CampaignMessage(campaign_id=32, dialog_id=51, account_id=22, kind="incoming", body="Ок, давайте",
                                   dedupe_key="in:9", status="received", telegram_message_id=9))
            await db.commit()
        self.run_db(incoming)
        self.assertIn("tg:51", self.keys(headers=AS_CLIENT))

    def test_telegram_reply_without_proxy_is_blocked(self):
        full = self.client.get(f"{BASE}/inbox/tg:52", headers=AS_CLIENT).json()
        self.assertFalse(full["can_reply"])
        self.assertIn("прокси", full["reply_blocker"])
        r = self.client.post(f"{BASE}/inbox/tg:52/reply", headers=AS_CLIENT, json={"text": "Привет"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.tg_sent, [])

    def test_email_reply_uses_template_variables(self):
        r = self.client.post(f"{BASE}/reply-templates", headers=ORIGIN,
                             json={"title": "Цены", "text": "Добрый день! Для {{company}} — прайс. {{sender_name}}"})
        self.assertEqual(r.status_code, 201, r.text)
        tid = r.json()["id"]
        self.assertEqual(self.client.get(f"{BASE}/reply-templates", headers=AS_CLIENT).json(), [])   # по клиенту
        self.assertEqual(self.client.delete(f"{BASE}/reply-templates/{tid}", headers=AS_CLIENT).status_code, 404)
        r = self.client.post(f"{BASE}/inbox/email:301/reply", headers=ORIGIN,
                             json={"text": "Добрый день! Для {{company}} — прайс. {{sender_name}}", "template_id": tid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.transport.sent[-1].body, "Добрый день! Для Кухни Плюс — прайс. Егор")
        self.assertEqual(self.transport.sent[-1].sender, "egor@m201.ru")
        full = self.client.get(f"{BASE}/inbox/email:301").json()
        self.assertEqual([m["author"]["kind"] for m in full["messages"]], ["lead", "agency"])
        self.assertTrue(full["handled"])

    def test_labels_have_effects_in_telegram(self):
        r = self.client.patch(f"{BASE}/inbox/tg:52", headers=AS_CLIENT, json={"label": "interested"})
        self.assertEqual((r.json()["label"], r.json()["hot"], r.json()["handled"]), ("interested", True, True))
        r = self.client.patch(f"{BASE}/inbox/tg:51", headers=AS_CLIENT, json={"label": "unsubscribe"})
        self.assertEqual(r.json()["effect"], "closed")

        async def check(db):
            return (await db.get(CampaignDialog, 51)).state, await db.get(GlobalBlock, 9001) is not None
        self.assertEqual(self.run_db(check), ("stopped", True))
        # Метка — папка: рассылка остановлена, но человек ответить может; диалог переходит в ручной.
        self.assertEqual(self.client.post(f"{BASE}/inbox/tg:51/reply", headers=AS_CLIENT,
                                          json={"text": "Ещё раз"}).status_code, 200)
        self.assertEqual(self.run_db(check), ("manual", True))
        r = self.client.patch(f"{BASE}/inbox/email:301", headers=ORIGIN, json={"label": "interested"})
        self.assertEqual((r.status_code, r.json()["label"]), (200, "interested"))
        self.assertEqual(self.client.patch(f"{BASE}/inbox/tg:52", headers=AS_CLIENT,
                                           json={"label": "spam"}).status_code, 422)

    def test_suggest_uses_agency_ai(self):
        calls = []

        async def fake(system, messages, **kw):
            calls.append(kw)
            return "  Да, пришлю прайс сегодня.  "
        outreach_inbox.complete = fake
        r = self.client.post(f"{BASE}/inbox/tg:51/suggest", headers=AS_CLIENT)
        self.assertEqual(r.json(), {"text": "Да, пришлю прайс сегодня."})
        self.assertEqual((calls[0]["bucket"], calls[0]["workspace_id"]), ("agency", CLIENT))

    def test_telegram_to_crm_creates_deal_once(self):
        r = self.client.post(f"{BASE}/inbox/tg:51/to-crm", headers=AS_CLIENT)
        self.assertEqual(r.status_code, 200, r.text)
        deal = r.json()["deal_id"]
        self.assertEqual(self.client.post(f"{BASE}/inbox/tg:51/to-crm", headers=AS_CLIENT).json()["deal_id"], deal)
        self.assertEqual(self.client.get(f"{BASE}/inbox/tg:51", headers=AS_CLIENT).json()["crm"]["deal_id"], deal)

    def test_failed_before_send_is_definite_and_uncertain_can_be_verified(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.services import telegram_jobs
        outreach_inbox.telegram_send = None

        async def busy(account, fn):
            raise RuntimeError("Аккаунт сейчас занят другой задачей")
        with patch.object(telegram_jobs, "_with_account", busy):
            r = self.client.post(f"{BASE}/inbox/tg:51/reply", headers=AS_CLIENT, json={"text": "Цена 9 900"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("НЕ отправлено", r.json()["detail"])

        class Client:
            async def send_message(self, *a, **k):
                raise ConnectionError("reset")

            async def get_messages(self, *a, **k):
                return [SimpleNamespace(out=True, message="Цена 9 900", id=77), SimpleNamespace(out=False, message="?", id=7)]

        async def connected(account, fn):
            return await fn(Client())
        with patch.object(telegram_jobs, "_with_account", connected):
            r = self.client.post(f"{BASE}/inbox/tg:51/reply", headers=AS_CLIENT, json={"text": "Цена 9 900"})
            self.assertEqual(r.status_code, 502)
            full = self.client.get(f"{BASE}/inbox/tg:51", headers=AS_CLIENT).json()
            self.assertIn("unknown", [m["status"] for m in full["messages"]])
            r = self.client.post(f"{BASE}/inbox/tg:51/verify", headers=AS_CLIENT)
        self.assertEqual((r.json()["sent"], r.json()["not_sent"]), (1, 0))
        self.assertIn("sent", [m["status"] for m in r.json()["conversation"]["messages"] if m["author"]["kind"] == "agency"])

    def test_rich_reply_formats_and_attaches_file(self):
        from unittest.mock import patch
        from app.services import telegram_jobs
        outreach_inbox.telegram_send = None
        calls = []

        class Client:
            async def send_file(self, peer, stream, **k):
                calls.append(("file", stream.name, stream.read(), k))
                return type("M", (), {"id": 88})()

            async def send_message(self, peer, text, **k):
                calls.append(("text", text, k))
                return type("M", (), {"id": 89})()

        async def connected(account, fn):
            return await fn(Client())
        with patch.object(telegram_jobs, "_with_account", connected):
            r = self.client.post(f"{BASE}/inbox/tg:51/reply-rich", headers=AS_CLIENT,
                                 data={"text": "**Цена** 9 900"}, files={"file": ("../прайс.pdf", b"%PDF", "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["text"], "Цена 9 900\n📎 прайс.pdf")
        kind, name, data, k = calls[0]
        self.assertEqual((kind, name, data, k["caption"]), ("file", "прайс.pdf", b"%PDF", "Цена 9 900"))
        self.assertEqual(type(k["formatting_entities"][0]).__name__, "MessageEntityBold")
        r = self.client.post(f"{BASE}/inbox/tg:51/reply-rich", headers=AS_CLIENT, data={"text": " "})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
