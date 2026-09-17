"""TData connection retries are isolated and never contact Telegram."""
import unittest
from unittest.mock import AsyncMock, patch

from app.services.tdata_import import TelegramConnectionError, _connect_with_retries


class FakeClient:
    def __init__(self, effects):
        self.connect = AsyncMock(side_effect=effects)
        self.disconnect = AsyncMock()


class ConnectionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_proxy_failure(self):
        client = FakeClient([ConnectionError("bad exit"), None])
        with patch("app.services.tdata_import.asyncio.sleep", new=AsyncMock()):
            await _connect_with_retries(client)

        self.assertEqual(client.connect.await_count, 2)
        client.disconnect.assert_awaited_once()

    async def test_reports_failure_after_all_attempts(self):
        client = FakeClient(ConnectionError("proxy refused"))
        with patch("app.services.tdata_import.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(TelegramConnectionError, "после 3 попыток"):
                await _connect_with_retries(client)

        self.assertEqual(client.connect.await_count, 3)
        self.assertEqual(client.disconnect.await_count, 3)


if __name__ == "__main__":
    unittest.main()
