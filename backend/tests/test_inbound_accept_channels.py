"""Requests from Telegram profiles, MAX and mail can be accepted even without a phone: the chat is the contact."""
from sqlalchemy import select

import test_messaging
from app.models.crm import CrmInbound
from app.models.messaging import MessagingChannel
from app.services import messaging
from app.services.messaging import Incoming
from datetime import datetime, timezone

ORIGIN = test_messaging.ORIGIN


class InboundAcceptChannelsTests(test_messaging.MessagingTests):
    def accept_from(self, kind, item):
        async def ingest(db):
            channel = MessagingChannel(workspace_id=1, project_id=1, kind=kind, name=kind, config={}, status="connected", active=True)
            db.add(channel); await db.flush()
            await messaging.ingest(db, channel, item)
            await db.commit()
        self.read(ingest)
        inbound_id = self.read(lambda db: db.scalar(select(CrmInbound.id).order_by(CrmInbound.id.desc())))
        return self.client.post(f"/api/crm/inbound/{inbound_id}/accept", headers=ORIGIN, json={})

    def test_telegram_profile_without_phone(self):
        item = Incoming(chat_id="777", title="potok", text="Здравствуйте", external_id="m1", sent_at=datetime.now(timezone.utc),
                        direction="in", phone=None, meta={"username": "grassuy", "telegram_id": 777})
        accepted = self.accept_from("telegram_user", item)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        deal = self.client.get(f"/api/crm/deals/{accepted.json()['deal_id']}").json()
        self.assertEqual(deal["contact"]["telegram"], "@grassuy")

    def test_max_without_phone(self):
        item = Incoming(chat_id="10000000", title="MAX 10000000", text="Добрый день", external_id="x1",
                        sent_at=datetime.now(timezone.utc), direction="in", phone=None, meta={})
        self.assertEqual(self.accept_from("max", item).status_code, 200)
