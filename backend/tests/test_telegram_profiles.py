import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from telethon import types
from telethon.errors import FloodWaitError, PeerFloodError

import test_messaging
from app.models.crm import CrmContact
from app.models.messaging import Conversation, MessagingChannel
from app.models.proxy import Proxy
from app.services import client_proxies, telegram_profiles


class ProfilesTests(test_messaging.MessagingTests):
    def test_proxy_capacity_dedicated_sticky_and_waiting(self):
        async def run(db):
            db.add_all([
                Proxy(id=1, host="one", port=1080, pool="client", status="working", max_accounts=3),
                Proxy(id=2, host="two", port=1080, pool="client", status="working", max_accounts=3,
                      dedicated_workspace_id=1),
            ])
            await db.flush()
            for index in range(4):
                channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user",
                                           name=f"Profile{index}", config={})
                db.add(channel)
                await db.flush()
                allocated = await client_proxies.allocate(db, channel)
                if index < 3:
                    self.assertEqual(allocated.id, 2)
                    allocated.status = "error"
                    self.assertEqual((await client_proxies.allocate(db, channel)).id, 2)
                    allocated.status = "working"
                else:
                    self.assertIsNone(allocated)
                    self.assertEqual(channel.status, "waiting_proxy")
        self.read(run)

    def test_least_loaded_pool_excludes_agency(self):
        async def run(db):
            db.add_all([Proxy(id=i, host=str(i), port=1080, pool="client" if i > 1 else "agency",
                              status="working", max_accounts=3) for i in range(1, 4)])
            await db.flush()
            for expected in [2, 3, 2]:
                channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user", name="Профиль")
                db.add(channel)
                await db.flush()
                self.assertEqual((await client_proxies.allocate(db, channel)).id, expected)
        self.read(run)

    def test_personal_filter_private_humans_and_mobile_outbound(self):
        async def run(db):
            channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user", name="Личный",
                                       config={"mode": "personal"})
            db.add(channel)
            await db.flush()
            sender = types.User(id=55, first_name="Анна", username="anna")
            event = SimpleNamespace(is_private=True, out=False, get_sender=AsyncMock(return_value=sender),
                                    get_chat=AsyncMock(return_value=sender), chat_id=55, id=1,
                                    raw_text="Привет", date=datetime.now(timezone.utc))
            self.assertFalse(await telegram_profiles.accept_event(db, channel, event))
            db.add(CrmContact(workspace_id=1, project_id=1, name="Анна", telegram="@anna"))
            await db.flush()
            self.assertTrue(await telegram_profiles.accept_event(db, channel, event))
            event.out, event.id = True, 2
            self.assertEqual((await telegram_profiles.accept_event(db, channel, event)).direction, "out")
            event.is_private = False
            self.assertFalse(await telegram_profiles.accept_event(db, channel, event))
            event.is_private, sender.bot = True, True
            self.assertFalse(await telegram_profiles.accept_event(db, channel, event))
            sender.bot, sender.is_self = False, True
            self.assertFalse(await telegram_profiles.accept_event(db, channel, event))
            self.assertEqual(len(list(await db.scalars(select(Conversation)))), 1)
        self.read(run)

    def test_telegram_errors_are_russian(self):
        self.assertIn("до", telegram_profiles.error_text(FloodWaitError(request=None, capture=120)))
        self.assertIn("спамблок", telegram_profiles.error_text(PeerFloodError(request=None)))

    def test_lease_has_only_one_owner(self):
        class Redis:
            owner = None
            async def set(self, key, owner, nx, ex):
                self.assertions = (key, nx, ex)
                if self.owner is not None:
                    return False
                self.owner = owner
                return True
        async def run():
            redis = Redis()
            self.assertTrue(await telegram_profiles.acquire(redis, "first"))
            self.assertFalse(await telegram_profiles.acquire(redis, "second"))
            self.assertEqual(redis.owner, "first")
            self.assertEqual(redis.assertions, (telegram_profiles.LEASE, True, 60))
        asyncio.run(run())

    def test_login_code_and_2fa_encrypt_session(self):
        async def run(db):
            proxy = Proxy(host="one", port=1080, pool="client", status="working")
            channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user", name="Профиль",
                                       config={"phone": "+79001234567"})
            db.add_all([proxy, channel])
            await db.flush()
            client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
                send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash")),
                sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=55, username="anna")),
                session=SimpleNamespace(save=lambda: "private-session"))
            with patch.object(telegram_profiles, "client_for", return_value=client):
                await telegram_profiles.login(db, channel)
                self.assertEqual(channel.status, "code_required")
                self.assertNotIn("private-session", channel.secret_encrypted)
                await telegram_profiles.login(db, channel, password="secret")
                self.assertEqual(channel.status, "connected")
                self.assertEqual(channel.config["telegram_user_id"], 55)
        self.read(run)
