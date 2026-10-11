"""MAX regular account through GREEN-API: connect, parse, write first by phone."""
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import test_stage2
from app.models.messaging import Conversation, MessagingChannel
from app.services import messaging
from app.services.messaging import MaxGreen

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class MaxChannelTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db, lead = Base.setUp, Base.tearDown, Base.as_user, Base.run_db, Base.lead

    def connect(self):
        self.as_user(1)
        with patch.object(MaxGreen, "test", AsyncMock(return_value={"name": "MAX подключён"})):
            created = self.client.post("/api/crm/projects/1/channels", headers=ORIGIN, json={
                "kind": "max", "name": "MAX продаж", "instance_id": "3100000001", "token": "d75b3a66374942c5b3c0"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["status"], "connected")
        return created.json()["id"]

    def test_connect_uses_max_api_and_parses_personal_chats_only(self):
        channel_id = self.connect()
        channel = self.run_db(lambda db: db.get(MessagingChannel, channel_id))
        self.assertEqual(channel.config["api_url"], "https://api.green-api.com/v3")
        self.assertEqual(MaxGreen(channel).url("sendMessage"), "https://api.green-api.com/v3/waInstance3100000001/sendMessage/d75b3a66374942c5b3c0")
        base = {"idMessage": "M1", "timestamp": 1791700000,
                "senderData": {"chatId": "10000000", "senderName": "Ольга", "senderPhoneNumber": "79270000001"}}
        incoming = MaxGreen.parse({**base, "typeWebhook": "incomingMessageReceived",
                                   "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Привет"}}})
        self.assertEqual((incoming["chat_id"], incoming["text"], incoming["phone"], incoming["direction"]),
                         ("10000000", "Привет", "+79270000001", "in"))
        group = {**base, "senderData": {"chatId": "-70000001", "senderName": "Чат"}}
        self.assertIsNone(MaxGreen.parse({**group, "typeWebhook": "incomingMessageReceived", "messageData": {"typeMessage": "textMessage"}}))
        self.assertIsNone(MaxGreen.parse({**base, "typeWebhook": "stateInstanceChanged"}))
        self.assertEqual(messaging.KINDS["max"], "MAX")

    def test_write_first_by_phone_and_history(self):
        channel_id = self.connect()
        deal_id = self.lead("Ольга", "89270000001", "max1")

        async def fake(method, url, **kwargs):
            if url.split("/")[-2] == "checkAccount":
                return {"exist": True, "chatId": "10000000"}
            if url.split("/")[-2] == "getChatHistory":
                return [{"type": "incoming", "idMessage": "H2", "timestamp": 1791700100, "textMessage": "Жду ответ"},
                        {"type": "outgoing", "idMessage": "H1", "timestamp": 1791700000, "textMessage": "Здравствуйте"}]
            return None
        with patch.object(messaging, "_json", side_effect=fake):
            started = self.client.post(f"/api/crm/deals/{deal_id}/conversations", headers=ORIGIN, json={"channel_id": channel_id})
            self.assertEqual(started.status_code, 201, started.text)
            channel = self.run_db(lambda db: db.get(MessagingChannel, channel_id))
            history = self.run_db(lambda db: MaxGreen(channel).history("10000000"))
        row = self.run_db(lambda db: db.scalar(select(Conversation).where(Conversation.channel_id == channel_id)))
        self.assertEqual((row.external_chat_id, row.deal_id), ("10000000", deal_id))
        self.assertEqual([h["text"] for h in history], ["Здравствуйте", "Жду ответ"])
        self.assertEqual([h["direction"] for h in history], ["out", "in"])

        async def missing(method, url, **kwargs):
            return {"exist": False}
        other = self.lead("Пётр", "89270000002", "max2")
        with patch.object(messaging, "_json", side_effect=missing):
            nope = self.client.post(f"/api/crm/deals/{other}/conversations", headers=ORIGIN, json={"channel_id": channel_id})
        self.assertEqual(nope.status_code, 404)
        self.assertIn("нет аккаунта MAX", nope.json()["detail"])
