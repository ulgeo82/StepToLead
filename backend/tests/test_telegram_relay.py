import asyncio
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services import messaging, notifications, telegram_http


class FakeResponse:
    status_code = 200
    content = b'{"ok": true, "result": {"message_id": 1}}'

    def json(self):
        return {"ok": True, "result": {"message_id": 1}}


class FakeClient:
    calls = []

    def __init__(self, **kwargs):
        FakeClient.calls.append({"init": kwargs})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        FakeClient.calls.append({"url": url})
        return FakeResponse()

    async def request(self, method, url, **kwargs):
        FakeClient.calls.append({"url": url})
        return FakeResponse()


class TelegramRelayTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []

    def test_default_goes_directly_to_telegram(self):
        with patch.object(settings, "telegram_api_base", "https://api.telegram.org"), patch.object(settings, "telegram_proxy", ""):
            self.assertEqual(telegram_http.bot_url("T", "getMe"), "https://api.telegram.org/botT/getMe")
            self.assertEqual(telegram_http.client_options(), {})

    def test_notification_bot_uses_relay_and_proxy(self):
        with patch.object(settings, "telegram_api_base", "https://relay.example.com/"), \
             patch.object(settings, "telegram_proxy", "http://user:pass@proxy.example.com:3128"), \
             patch.object(settings, "telegram_bot_token", "123:ABC"), \
             patch.object(notifications.httpx, "AsyncClient", FakeClient):
            asyncio.run(notifications.telegram_api("getMe"))
        self.assertEqual(FakeClient.calls[0]["init"]["proxy"], "http://user:pass@proxy.example.com:3128")
        self.assertEqual(FakeClient.calls[1]["url"], "https://relay.example.com/bot123:ABC/getMe")

    def test_client_bot_uses_relay_but_other_channels_do_not(self):
        channel = type("Channel", (), {})()
        with patch.object(settings, "telegram_api_base", "https://relay.example.com"), \
             patch.object(settings, "telegram_proxy", "socks5://proxy.example.com:1080"), \
             patch.object(settings, "telegram_bot_token", ""), \
             patch.object(messaging, "secret", lambda ch: "777:XYZ"), \
             patch.object(messaging.httpx, "AsyncClient", FakeClient):
            asyncio.run(messaging.TelegramBot(channel).call("getMe"))
            asyncio.run(messaging._json("GET", "https://other.example.com/api"))
        self.assertEqual(FakeClient.calls[0]["init"]["proxy"], "socks5://proxy.example.com:1080")
        self.assertEqual(FakeClient.calls[1]["url"], "https://relay.example.com/bot777:XYZ/getMe")
        self.assertNotIn("proxy", FakeClient.calls[2]["init"])


if __name__ == "__main__":
    unittest.main()
