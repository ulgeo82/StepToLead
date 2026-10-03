"""Inbox: channel connect, incoming → request + conversation, replies, linking to deals, WhatsApp parsing."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import api_router
from app.core.access import PORTAL_COOKIE, hash_password, token_digest
from app.db import Base, get_db
from app.models.crm import CrmInbound
from app.models.marketing import ClientWorkspace, PortalProjectAccess, PortalSession, PortalUser, Project
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import messaging
from app.services.messaging import Incoming, TelegramBot, WhatsAppGreen

ORIGIN = {"Origin": "http://localhost:3000"}


def tg(message_id: int, text: str, chat="555", name="Анна"):
    return Incoming(chat_id=chat, title=name, text=text, external_id=f"m{message_id}",
                    sent_at=datetime.now(timezone.utc), direction="in", phone=None, meta={"username": "anna_k"})


class MessagingTests(unittest.TestCase):
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
                for uid, role in ((1, "client_owner"), (2, "sales_manager"), (3, "sales_manager")):
                    db.add(PortalUser(id=uid, workspace_id=1, username=f"u{uid}", display_name=f"User {uid}",
                                      password_hash=hash_password("test-password-123"), role=role, must_change_password=False))
                    db.add(PortalSession(token_hash=token_digest(str(uid) * 43), user_id=uid,
                                         expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)))
                await db.flush()
                db.add_all([PortalProjectAccess(user_id=2, project_id=1), PortalProjectAccess(user_id=3, project_id=1)])
                await db.commit()
        asyncio.run(seed())

        async def database():
            async with self.sessions() as db:
                yield db
        app = FastAPI(); app.include_router(api_router, prefix="/api")
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.as_user(1)

    def tearDown(self):
        self.client.close(); asyncio.run(self.engine.dispose()); os.unlink(self.path)

    def as_user(self, uid):
        self.client.cookies.clear(); self.client.cookies.set(PORTAL_COOKIE, str(uid) * 43)

    def read(self, fn):
        async def run():
            async with self.sessions() as db:
                return await fn(db)
        return asyncio.run(run())

    def connect_telegram(self):
        with patch.object(TelegramBot, "test", AsyncMock(return_value={"name": "@shop_bot"})):
            created = self.client.post("/api/crm/projects/1/channels", headers=ORIGIN,
                                       json={"kind": "telegram_bot", "name": "Бот магазина", "token": "123456:ABCDEFGHIJ"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["status"], "connected")
        return created.json()["id"]

    def poll(self, channel_id, items):
        async def run(db):
            channel = await db.get(MessagingChannel, channel_id)
            with patch.object(TelegramBot, "poll", AsyncMock(return_value=items)):
                return await messaging.poll_channel(db, channel)
        return self.read(run)

    def test_incoming_creates_request_conversation_and_dedupes(self):
        channel_id = self.connect_telegram()
        self.assertEqual(self.poll(channel_id, [tg(1, "Здравствуйте, сколько стоит кухня?"), tg(2, "3 метра")]), 2)
        self.assertEqual(self.poll(channel_id, [tg(2, "3 метра")]), 0)
        conversations = self.client.get("/api/crm/projects/1/conversations").json()
        self.assertEqual(len(conversations["items"]), 1)
        item = conversations["items"][0]
        self.assertEqual((item["unread_count"], item["channel_kind"]), (2, "telegram_bot"))
        self.assertIsNotNone(item["waiting_since"])
        self.assertEqual(conversations["counts"]["waiting"], 1)
        inbound = self.read(lambda db: db.scalar(select(CrmInbound)))
        self.assertEqual(inbound.raw_payload["contact"], "@anna_k")
        self.assertEqual(inbound.raw_payload["notes"], "Здравствуйте, сколько стоит кухня?")
        detail = self.client.get(f"/api/crm/conversations/{item['id']}").json()
        self.assertEqual([m["text"] for m in detail["messages"]], ["Здравствуйте, сколько стоит кухня?", "3 метра"])
        self.assertEqual(self.client.get("/api/crm/projects/1/conversations").json()["items"][0]["unread_count"], 0)

    def test_reply_accept_link_and_failure(self):
        channel_id = self.connect_telegram()
        self.poll(channel_id, [tg(1, "Хочу кухню")])
        conversation_id = self.read(lambda db: db.scalar(select(Conversation.id)))
        inbound_id = self.read(lambda db: db.scalar(select(CrmInbound.id)))
        accepted = self.client.post(f"/api/crm/inbound/{inbound_id}/accept", headers=ORIGIN, json={})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        deal_id = accepted.json()["deal_id"]
        linked = self.client.get(f"/api/crm/deals/{deal_id}/conversations").json()
        self.assertEqual([c["id"] for c in linked], [conversation_id])
        self.assertEqual(self.client.get(f"/api/crm/deals/{deal_id}").json()["contact"]["telegram"], "@anna_k")
        with patch.object(TelegramBot, "send", AsyncMock(return_value="m99")):
            sent = self.client.post(f"/api/crm/conversations/{conversation_id}/messages", headers=ORIGIN, json={"text": "Добрый день! Удобно созвониться?"})
        self.assertEqual(sent.status_code, 201, sent.text)
        detail = self.client.get(f"/api/crm/conversations/{conversation_id}").json()
        self.assertIsNone(detail["waiting_since"])
        deal = self.client.get(f"/api/crm/deals/{deal_id}").json()
        self.assertIn("MESSAGE_SENT", [a["event_type"] for a in deal["activities"]])
        self.assertIsNotNone(deal["first_response_at"])
        with patch.object(TelegramBot, "send", AsyncMock(side_effect=messaging.ChannelError("Forbidden: bot was blocked by the user"))):
            failed = self.client.post(f"/api/crm/conversations/{conversation_id}/messages", headers=ORIGIN, json={"text": "Ау"})
        self.assertEqual(failed.status_code, 422)
        statuses = self.read(lambda db: db.scalars(select(Message.status).where(Message.direction == "out").order_by(Message.id)))
        self.assertEqual(list(statuses), ["sent", "failed"])

    def test_managers_see_unassigned_and_own_only(self):
        channel_id = self.connect_telegram()
        self.poll(channel_id, [tg(1, "Первый", chat="1"), tg(2, "Второй", chat="2")])
        first, second = self.read(lambda db: db.scalars(select(Conversation.id).order_by(Conversation.id)))
        self.client.patch(f"/api/crm/conversations/{first}", headers=ORIGIN, json={"assigned_user_id": 3})
        self.as_user(2)
        visible = [c["id"] for c in self.client.get("/api/crm/projects/1/conversations").json()["items"]]
        self.assertEqual(visible, [second])
        self.assertEqual(self.client.get(f"/api/crm/conversations/{first}").status_code, 403)
        self.assertEqual(self.client.post("/api/crm/projects/1/channels", headers=ORIGIN,
                                          json={"kind": "telegram_bot", "name": "Бот", "token": "1234567890"}).status_code, 403)

    def test_whatsapp_parse(self):
        base = {"timestamp": 1588091580, "idMessage": "ABC", "senderData": {"chatId": "79001234567@c.us", "senderName": "Иван"}}
        incoming = WhatsAppGreen.parse({**base, "typeWebhook": "incomingMessageReceived",
                                        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Привет"}}})
        self.assertEqual((incoming["chat_id"], incoming["phone"], incoming["text"], incoming["direction"]),
                         ("79001234567@c.us", "+79001234567", "Привет", "in"))
        outgoing = WhatsAppGreen.parse({**base, "typeWebhook": "outgoingMessageReceived",
                                        "messageData": {"typeMessage": "extendedTextMessage", "extendedTextMessageData": {"text": "Ссылка"}}})
        self.assertEqual((outgoing["direction"], outgoing["text"]), ("out", "Ссылка"))
        self.assertIsNone(WhatsAppGreen.parse({**base, "typeWebhook": "incomingMessageReceived",
                                               "senderData": {"chatId": "123@g.us"}, "messageData": {}}))
        self.assertIsNone(WhatsAppGreen.parse({**base, "typeWebhook": "stateInstanceChanged"}))
        photo = WhatsAppGreen.parse({**base, "typeWebhook": "incomingMessageReceived", "messageData": {"typeMessage": "imageMessage"}})
        self.assertEqual(photo["text"], "📷 Фото")


if __name__ == "__main__":
    unittest.main()
