"""Telegram profile login by QR code and per-client proxies entered by the agency in the client card."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import SessionPasswordNeededError

import test_stage2
from app.core.access import COOKIE
from app.core.crypto import decrypt_secret
from app.models.messaging import MessagingChannel
from app.models.proxy import Proxy
from app.services import client_proxies, telegram_profiles
from app.services.messaging import ChannelError

ORIGIN = test_stage2.ORIGIN
Base = test_stage2.Stage2Tests


class FakeQr:
    def __init__(self, outcome):
        self.outcome, self.url = outcome, "tg://login?token=AAA"
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def wait(self, timeout=None):
        await asyncio.sleep(0.05)
        if self.outcome is not None:
            raise self.outcome

    async def recreate(self):
        self.url = "tg://login?token=BBB"


def fake_client(outcome=None, user_id=42):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.qr_login = AsyncMock(return_value=FakeQr(outcome))
    client.get_me = AsyncMock(return_value=SimpleNamespace(id=user_id, username="sales", phone="79270000001"))
    client.sign_in = AsyncMock()
    client.session.save.return_value = "SESSION-STRING"
    return client


class TelegramQrTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def channel(self, db, **config):
        channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user", name="Продажи",
                                   config={"mode": "corporate", **config}, active=False, status="qr_pending")
        db.add(channel)
        return channel

    def play(self, scenario):
        async def go():
            async with self.sessions() as db:
                db.add(Proxy(id=5, host="10.0.0.1", port=1080, pool="client", status="working", dedicated_workspace_id=1))
                await db.commit()
            with patch.object(telegram_profiles, "SessionLocal", self.sessions):
                return await scenario()
        return asyncio.run(go())

    def test_qr_login_saves_session_and_shows_svg(self):
        client = fake_client()

        async def scenario():
            async with self.sessions() as db:
                channel = self.channel(db); await db.flush()
                with patch.object(telegram_profiles, "make_client", return_value=client):
                    state = await telegram_profiles.start_qr(db, channel)
                await db.commit()
                self.assertEqual(state["status"], "waiting")
                self.assertIn("<svg", state["svg"])
                await telegram_profiles.qr_logins[channel.id]["task"]
                return channel.id
        channel_id = self.play(scenario)
        row = self.run_db(lambda db: db.get(MessagingChannel, channel_id))
        self.assertEqual((row.status, row.active, row.config["telegram_user_id"], row.config["phone"], row.config["proxy_id"]),
                         ("connected", True, 42, "+79270000001", 5))
        self.assertEqual(decrypt_secret(row.secret_encrypted), "SESSION-STRING")
        self.assertNotIn(channel_id, telegram_profiles.qr_logins)
        client.disconnect.assert_awaited()

    def test_cloud_password_then_connected(self):
        client = fake_client(SessionPasswordNeededError(request=None))

        async def scenario():
            async with self.sessions() as db:
                channel = self.channel(db); await db.flush()
                with patch.object(telegram_profiles, "make_client", return_value=client):
                    await telegram_profiles.start_qr(db, channel)
                await db.commit()
            for _ in range(50):
                if telegram_profiles.qr_state(channel.id)["status"] == "password_required":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(telegram_profiles.qr_state(channel.id)["status"], "password_required")
            result = await telegram_profiles.qr_password(channel.id, "secret-pass")
            client.sign_in.assert_awaited_with(password="secret-pass")
            return channel.id, result
        channel_id, result = self.play(scenario)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(self.run_db(lambda db: db.get(MessagingChannel, channel_id)).status, "connected")

    def test_no_proxy_and_duplicate_account(self):
        async def scenario():
            async with self.sessions() as db:
                await db.delete(await db.get(Proxy, 5))
                channel = self.channel(db); await db.flush()
                with self.assertRaises(ChannelError) as raised:
                    await telegram_profiles.start_qr(db, channel)
                self.assertIn("не настроен прокси", str(raised.exception))
                db.add(Proxy(id=6, host="10.0.0.2", port=1080, pool="client", status="working", dedicated_workspace_id=1))
                self.channel(db, telegram_user_id=42).status = "connected"
                await db.flush()
                with patch.object(telegram_profiles, "make_client", return_value=fake_client()):
                    await telegram_profiles.start_qr(db, channel)
                await db.commit()
                await telegram_profiles.qr_logins[channel.id]["task"]
                return channel.id
        channel_id = self.play(scenario)
        row = self.run_db(lambda db: db.get(MessagingChannel, channel_id))
        self.assertEqual(row.status, "error")
        self.assertIn("уже подключён", row.last_error)
        self.assertIsNone(row.secret_encrypted)


class ClientProxyAdminTests(unittest.TestCase):
    setUp, tearDown, as_user, run_db = Base.setUp, Base.tearDown, Base.as_user, Base.run_db

    def admin(self):
        self.client.cookies.clear(); self.client.cookies.set(COOKIE, "a" * 43)

    def test_agency_adds_checks_and_replaces_client_proxy(self):
        self.admin()
        with patch.object(client_proxies, "check", AsyncMock(side_effect=OSError("down"))):
            bad = self.client.post("/api/proxies/workspace/1", headers=ORIGIN, json={"line": "1.2.3.4:1080:u:p"})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("не пропускает к Telegram", bad.json()["detail"])
        self.assertEqual(self.client.post("/api/proxies/workspace/1", headers=ORIGIN,
                                          json={"line": "socks4://1.2.3.4:1080"}).status_code, 422)
        with patch.object(client_proxies, "check", AsyncMock()):
            added = self.client.post("/api/proxies/workspace/1", headers=ORIGIN, json={"line": "1.2.3.4:1080:u:p"})
        self.assertEqual(added.status_code, 201, added.text)
        proxy = added.json()["proxies"][0]
        self.assertEqual((proxy["host"], proxy["status"], proxy["max_accounts"]), ("1.2.3.4", "working", 3))

        async def bind(db):
            channel = MessagingChannel(workspace_id=1, project_id=1, kind="telegram_user", name="Продажи",
                                       config={"proxy_id": proxy["id"]}, status="error", last_error="Нет связи через прокси")
            db.add(channel); await db.commit()
            return channel.id
        channel_id = self.run_db(bind)
        before = self.run_db(lambda db: db.get(Proxy, proxy["id"]))
        old_signature = client_proxies.signature(before)
        with patch.object(client_proxies, "check", AsyncMock()):
            unconfirmed = self.client.put(f"/api/proxies/{proxy['id']}/replace", headers=ORIGIN, json={"line": "5.6.7.8:1080:u2:p2"})
            self.assertEqual(unconfirmed.status_code, 422)
            replaced = self.client.put(f"/api/proxies/{proxy['id']}/replace", headers=ORIGIN,
                                       json={"line": "5.6.7.8:1080:u2:p2", "confirm_ip_change": True})
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["moved"], 1)
        after = self.run_db(lambda db: db.get(Proxy, proxy["id"]))
        self.assertEqual((after.host, after.username, decrypt_secret(after.password_encrypted)), ("5.6.7.8", "u2", "p2"))
        self.assertNotEqual(client_proxies.signature(after), old_signature)
        channel = self.run_db(lambda db: db.get(MessagingChannel, channel_id))
        self.assertEqual((channel.status, channel.config["proxy_id"]), ("connected", proxy["id"]))
        listed = self.client.get("/api/proxies/workspace/1").json()
        self.assertEqual([p["name"] for p in listed["proxies"][0]["profiles"]], ["Продажи"])
        # Portal users cannot touch agency proxies.
        self.client.cookies.clear(); self.client.cookies.set(test_stage2.PORTAL_COOKIE, "1" * 43)
        self.assertIn(self.client.get("/api/proxies/workspace/1").status_code, {401, 403})


class HttpProxyTests(unittest.TestCase):
    def test_http_proxy_is_accepted_and_checked_as_http(self):
        seen = {}

        async def fake_check(proxy):
            seen["scheme"] = proxy.scheme
        with patch.object(client_proxies, "check", side_effect=fake_check):
            probe = asyncio.run(client_proxies.checked_from_line("http://user:pass@mobpool.example:10000"))
        self.assertEqual((probe.scheme, probe.host, probe.port, probe.username, seen["scheme"]),
                         ("http", "mobpool.example", 10000, "user", "http"))
        from app.services.telegram import proxy_config
        self.assertEqual(proxy_config(probe)["proxy_type"], "http")
