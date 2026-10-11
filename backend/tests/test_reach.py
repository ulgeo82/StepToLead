from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import test_messaging
from app.core.crypto import encrypt_secret
from app.models.crm import CrmContact, CrmDeal, CrmPipeline, CrmStage
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import messaging


class ReachTests(test_messaging.MessagingTests):
    def seed_deal(self, kinds=("whatsapp", "telegram_user")):
        async def run(db):
            db.add(CrmContact(id=1, workspace_id=1, project_id=1, name="Анна",
                              phones=["+79001234567"], emails=["anna@example.org"], telegram="@anna"))
            db.add(CrmPipeline(id=1, workspace_id=1, project_id=1, name="Продажи"))
            await db.flush()
            db.add(CrmStage(id=1, pipeline_id=1, name="Новая", analytics_type="NEW"))
            await db.flush()
            db.add(CrmDeal(id=1, workspace_id=1, project_id=1, contact_id=1, pipeline_id=1, stage_id=1,
                           name="Кухня", responsible_user_id=2))
            for index, kind in enumerate(kinds, 1):
                db.add(MessagingChannel(id=index, workspace_id=1, project_id=1, name=kind, kind=kind,
                                        status="connected", config={"instance_id": "123"},
                                        secret_encrypted=encrypt_secret("token")))
            await db.commit()
        self.read(run)

    def test_found_multiple_and_open_is_idempotent(self):
        self.seed_deal()
        found = {"external_chat_id": "55", "title": "Анна", "has_history": True}
        adapter = SimpleNamespace(lookup=AsyncMock(return_value=found), history=AsyncMock(return_value=[{
            "external_id": "m1", "direction": "in", "text": "Здравствуйте",
            "sent_at": datetime.now(timezone.utc)}]))
        with patch.object(messaging, "adapter", return_value=adapter):
            result = self.client.post("/api/crm/deals/1/reach/lookup", headers=test_messaging.ORIGIN)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([c["found"] for c in result.json()["items"]], [True, True])
            ids = []
            for _ in range(2):
                result = self.client.post("/api/crm/deals/1/reach/open", headers=test_messaging.ORIGIN,
                                          json={"channel_id": 1})
                self.assertEqual(result.status_code, 200, result.text)
                ids.append(result.json()["conversation_id"])
            self.assertEqual(ids[0], ids[1])
        rows = self.read(lambda db: db.scalars(select(Conversation)))
        self.assertEqual([(r.deal_id, r.contact_id) for r in rows], [(1, 1)])
        self.assertEqual(len(list(self.read(lambda db: db.scalars(select(Message))))), 1)

    def test_none_bot_only_and_deal_access(self):
        self.seed_deal(("telegram_bot", "avito"))
        result = self.client.post("/api/crm/deals/1/reach/lookup", headers=test_messaging.ORIGIN)
        self.assertIn("нельзя написать первым", result.json()["message"])
        self.assertFalse(any(c["found"] for c in result.json()["items"]))
        self.as_user(3)
        self.assertEqual(self.client.post("/api/crm/deals/1/reach/lookup",
                                          headers=test_messaging.ORIGIN).status_code, 403)

    def test_not_found(self):
        self.seed_deal()
        adapter = SimpleNamespace(lookup=AsyncMock(return_value=None))
        with patch.object(messaging, "adapter", return_value=adapter):
            result = self.client.post("/api/crm/deals/1/reach/lookup", headers=test_messaging.ORIGIN)
        self.assertFalse(any(c["found"] for c in result.json()["items"]))

    def test_whatsapp_lookup_and_history(self):
        channel = MessagingChannel(kind="whatsapp", config={"instance_id": "123"},
                                   secret_encrypted=encrypt_secret("token"))
        adapter = messaging.WhatsAppGreen(channel)
        rows = [{"idMessage": "m2", "type": "outgoing", "textMessage": "Здравствуйте", "timestamp": 1}]
        with patch.object(messaging, "_json", AsyncMock(side_effect=[{"existsWhatsapp": True}, rows])):
            found = self.read(lambda db: adapter.lookup(phone="+79001234567"))
        self.assertEqual(found["external_chat_id"], "79001234567@c.us")
        self.assertTrue(found["has_history"])
